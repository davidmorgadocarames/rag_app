"""p50/p95 latency per stage over the golden set — the "before" baseline (T11.3.5).

Drives ``generation.answer_question`` directly, once per golden item per run: the exact
shape the live API uses today, including a **fresh** ``CrossEncoderReranker()`` per call
(``reranking.retrieve``) — so this "before" picture also carries today's reload-per-request
bug (``rerank_load`` dominating every answer; fixed in T11.4.1, block C). This module is
reused unchanged for the "after" measurement there, so the two numbers are directly
comparable. Deliberately excludes the correctness judge (an extra, non-user-facing LLM call
that would also contend for the GPU/CPU and pollute the numbers — ``eval.benchmark`` notes
the same exclusion).

Per-stage durations come from the same ``rag_app.timing`` recorder the live API uses (an
``on_stage`` callback appends every stage/ttft/total duration), so a number here means
exactly the same thing as a number in the Prometheus histograms or the `answer_timing` log
line.

Writes ``eval/latency_baseline.json``: machine/model details (CPU, GPU, RAM, model names +
revisions, reranker device, `top_k`/`rerank_top_n`, git commit) alongside p50/p95 per stage,
so a number is never compared across machines or configurations by accident.

Called by ``scripts/gate.sh`` (step ``latency``) with ``--require-stack``. This block
(T11.3.5) only RECORDS — no pass/fail threshold yet (T11.6b.1, block G, compares a later run
against the file an earlier block "adopts" as the baseline).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from rag_app import timing
from rag_app.config import get_settings
from rag_app.db.session import make_engine, make_session_factory
from rag_app.eval.dataset import DEFAULT_GOLDEN_SET, GoldenItem, load_golden_set
from rag_app.llm import OllamaChat

REPO_ROOT = Path(__file__).resolve().parents[4]
EVAL_DIR = REPO_ROOT / "eval"
# Same split as eval/gate.py: `latency_baseline.json` is git-tracked (the "before"/"after"
# reference other blocks compare against) and only updated on purpose, via --update-baseline;
# a routine `gate.sh --full`/`--only latency` run writes the untracked `latency_results.json`
# instead, every time — otherwise run-to-run timing jitter would dirty the tree on every gate
# run and the gate-full commit status could never publish (D-2026-10-02, found running the
# final --full for this block: the tree had "uncommitted changes" after latency ran).
BASELINE_PATH = EVAL_DIR / "latency_baseline.json"
RESULTS_PATH = EVAL_DIR / "latency_results.json"

# N runs over the golden set (14 questions, 2026-10-02): each run pays a FRESH
# `CrossEncoderReranker()` load per question (today's bug) + a real Ollama generate +
# groundedness call per question. N=3 (42 answers total) keeps a local run in the
# few-minutes range (measured on the development machine, recorded below in the printed
# summary and in the written JSON's "runtime_s") while still giving p50/p95 some spread
# (42 samples: p95 is the ~40th-largest, not just the max).
DEFAULT_RUNS = 3

STAGE_NAMES = (
    "classify",
    "embed",
    "hybrid_search",
    "rerank_load",
    "rerank_inference",
    "generate",
    "groundedness",
    "ttft",
    "total",
)


@dataclass
class LatencyReport:
    machine: dict[str, object]
    n_runs: int
    n_questions: int
    runtime_s: float
    stages_ms: dict[str, dict[str, float]]  # stage -> {p50, p95, n, mean}

    def to_json(self) -> dict[str, object]:
        return {
            "machine": self.machine,
            "n_runs": self.n_runs,
            "n_questions": self.n_questions,
            "runtime_s": round(self.runtime_s, 1),
            "stages_ms": self.stages_ms,
        }


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no numpy dependency); ``values`` need not be pre-sorted."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - best effort, never fatal
        return "unknown"


def _gpu_info() -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        line = out.stdout.strip().splitlines()
        return line[0] if line else None
    except Exception:  # noqa: BLE001 - no GPU / no driver is a normal case
        return None


def _cpu_model() -> str:
    try:
        text_ = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for line in text_.splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _ram_gib() -> float:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        return (page_size * pages) / (1024**3)
    except (ValueError, OSError, AttributeError):
        return 0.0


def machine_info(*, reranker_device: str) -> dict[str, object]:
    settings = get_settings()
    return {
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "ram_gib": round(_ram_gib(), 1),
        "gpu": _gpu_info(),
        "reranker_device": reranker_device,
        "llm_model": settings.llm_model,
        "embed_model": settings.embed_model,
        "reranker_model": settings.reranker_model,
        "reranker_revision": settings.reranker_revision,
        "top_k": settings.top_k,
        "rerank_top_n": settings.rerank_top_n,
        "num_ctx_answer": settings.num_ctx_answer,
        "num_ctx_groundedness": settings.num_ctx_groundedness,
        "commit": _git_sha(),
    }


def run_latency_benchmark(
    session: Session,
    items: list[GoldenItem],
    *,
    n_runs: int = DEFAULT_RUNS,
    reranker_device: str = "cpu",
) -> LatencyReport:
    """Run the golden set ``n_runs`` times through the exact same calls
    ``generation.answer_question`` makes (a FRESH ``CrossEncoderReranker()`` per question —
    today's reload-per-request bug included — then ``answer_from_chunks``), collecting every
    stage duration via our OWN ``timing.recorder(on_stage=...)``.

    Deliberately inlined rather than calling ``answer_question`` itself: that function opens
    its own recorder with the Prometheus ``observe_stage`` callback hard-wired (correct for
    the live API), and a recorder cannot be nested — an outer one here would never see any
    stage duration (`timing.stage()` always reports to the innermost active recorder).
    """
    import time as _time

    from rag_app.generation import answer_from_chunks
    from rag_app.reranking import CrossEncoderReranker, retrieve

    chat = OllamaChat()
    durations_ms: dict[str, list[float]] = {name: [] for name in STAGE_NAMES}

    def on_stage(name: str, elapsed_s: float) -> None:
        durations_ms.setdefault(name, []).append(elapsed_s * 1000)

    start = _time.perf_counter()
    for _ in range(n_runs):
        for item in items:
            with timing.recorder(streaming=False, on_stage=on_stage):
                chunks = retrieve(
                    session, item.question, reranker=CrossEncoderReranker(), version=item.version
                )
                answer_from_chunks(chat, item.question, chunks)
    runtime_s = _time.perf_counter() - start

    stages_ms: dict[str, dict[str, float]] = {}
    for name, values in durations_ms.items():
        if not values:
            continue
        stages_ms[name] = {
            "n": len(values),
            "mean": round(sum(values) / len(values), 1),
            "p50": round(_percentile(values, 50), 1),
            "p95": round(_percentile(values, 95), 1),
        }

    return LatencyReport(
        machine=machine_info(reranker_device=reranker_device),
        n_runs=n_runs,
        n_questions=len(items),
        runtime_s=runtime_s,
        stages_ms=stages_ms,
    )


def _stack_available() -> bool:
    settings = get_settings()
    try:
        with make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return False
    try:
        httpx.get(f"{settings.ollama_host.rstrip('/')}/api/tags", timeout=5).raise_for_status()
    except Exception:  # noqa: BLE001
        return False
    return True


def _print_summary(report: LatencyReport) -> None:
    print(f"\nlatency baseline: {report.n_runs} run(s) x {report.n_questions} question(s)")
    print(f"machine: {report.machine}")
    header = f"{'stage':18} {'n':>4} {'mean_ms':>10} {'p50_ms':>10} {'p95_ms':>10}"
    print(header)
    for name in STAGE_NAMES:
        stats = report.stages_ms.get(name)
        if not stats:
            continue
        print(
            f"{name:18} {stats['n']:>4.0f} {stats['mean']:>10.1f}"
            f" {stats['p50']:>10.1f} {stats['p95']:>10.1f}"
        )
    print(f"runtime: {report.runtime_s:.1f} s\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record p50/p95 latency per stage.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument(
        "--device",
        choices=("cpu", "gpu"),
        default="cpu",
        help="reranker device label; 'cpu' also hides CUDA from torch before any model loads"
        " (Azure-relevant number) — a no-op with this repo's CURRENT CPU-only torch pin"
        " (requirements-torch.txt), kept for when T11.5.1's experiment matrix adds a GPU"
        " variant; 'gpu' leaves CUDA visible (local-dev-only number once that lands)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="defaults to latency_results.json, or latency_baseline.json with"
        " --update-baseline (same split as eval.gate's results.json/baseline_metrics.json)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write/overwrite the git-tracked latency_baseline.json (do this on purpose, not"
        " on every gate run — otherwise timing jitter would dirty the tree on every --full)",
    )
    parser.add_argument(
        "--require-stack",
        action="store_true",
        help="fail (instead of skipping) when Postgres/Ollama are not reachable (gate --full)",
    )
    args = parser.parse_args(argv)
    out_path = args.out or (BASELINE_PATH if args.update_baseline else RESULTS_PATH)

    if args.device == "cpu":
        # Must happen before any torch import (lazy, inside reranking.load_cross_encoder) —
        # argparse runs first, so this is still early enough.
        os.environ["CUDA_VISIBLE_DEVICES"] = ""

    if not _stack_available():
        if args.require_stack:
            settings = get_settings()
            print(
                "latency gate: FAIL — stack not reachable (Postgres or Ollama at"
                f" {settings.ollama_host}); start it or fix OLLAMA_HOST / DATABASE_URL",
                file=sys.stderr,
            )
            return 1
        print("latency gate: SKIP (Postgres/Ollama not reachable)")
        return 0

    items = load_golden_set(args.golden)
    session_factory = make_session_factory()
    with session_factory() as session:
        report = run_latency_benchmark(
            session, items, n_runs=args.runs, reranker_device=args.device
        )

    _print_summary(report)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    label = "baseline" if args.update_baseline else "results"
    print(f"latency {label} written -> {out_path}")
    print("\nLATENCY GATE: RECORDED (no pass/fail threshold yet — T11.6b.1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
