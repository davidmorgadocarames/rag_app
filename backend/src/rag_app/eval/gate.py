"""Evaluation gate: run the eval, enforce thresholds + no-regression, block on failure.

Called by scripts/gate.sh (step ``eval``, part of ``--full`` / ``--only eval``) with
``--require-stack``: an unreachable stack (Postgres + Ollama) then FAILS the gate. Run by
hand without that flag, an unreachable stack is reported as SKIP (exit 0).

**Golden set v2 (T14.0.4/T14.0.5, block 14-A2):** this same step also replays
``eval/golden_set_v2.jsonl`` (dialogues) and records the "before" baseline + the plan's floors
(``eval/golden_set_v2_baseline.json`` / ``eval/golden_set_v2_floors.json``, both git-tracked).
**Per PHASE_PLANNING §14.2's own Gate ("floors from 14.0") the floors are enforced starting at
14.2, once the router/memory they measure exists** — T14.0.5 establishes and records them, it
does not yet gate on them (``golden_set_v2_floors.json``'s ``enforce_now: false``); this keeps
the v1 eval gate's pass/fail behaviour above completely unaffected and green. See the ADR for the
numbers and why several are expected to fall short of their floor on this "before" run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from rag_app.config import get_settings
from rag_app.db.session import make_engine, make_session_factory
from rag_app.eval.dataset import (
    DEFAULT_GOLDEN_SET,
    DEFAULT_GOLDEN_SET_V2,
    load_dialogues,
    load_golden_set,
)
from rag_app.eval.metrics import DialogueMetrics, Metrics, compute_dialogue_metrics, compute_metrics
from rag_app.eval.runner import evaluate, evaluate_dialogues

REPO_ROOT = Path(__file__).resolve().parents[4]
EVAL_DIR = REPO_ROOT / "eval"
THRESHOLDS_PATH = EVAL_DIR / "thresholds.json"
BASELINE_PATH = EVAL_DIR / "baseline_metrics.json"
JUDGE_VALIDATION_PATH = EVAL_DIR / "judge_validation.json"
RESULTS_PATH = EVAL_DIR / "results.json"

V2_BASELINE_PATH = EVAL_DIR / "golden_set_v2_baseline.json"
V2_FLOORS_PATH = EVAL_DIR / "golden_set_v2_floors.json"
V2_RESULTS_PATH = EVAL_DIR / "golden_set_v2_results.json"

_REGRESSION_EPSILON = 0.02
_METRIC_KEYS = ("retrieval_recall", "faithfulness", "correctness", "correct_abstention")
# Ratio metrics: higher >= floor is "met". Latency metrics (lower <= floor is "met") are listed
# separately in _V2_LATENCY_KEYS so _check_v2 compares them the right way round.
_V2_METRIC_KEYS = (
    "intent_accuracy",
    "noisy_recall",
    "follow_up_resolution",
    "conversational_reply_rate",
)
_V2_LATENCY_KEYS = ("greeting_reply_p95_s",)


def _stack_available() -> bool:
    settings = get_settings()
    try:
        with make_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return False
    try:
        httpx.get(f"{settings.ollama_host.rstrip('/')}/api/tags", timeout=5).raise_for_status()
    except Exception:
        return False
    return True


def _load_json(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data: dict[str, float] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _judge_validation_failure() -> str | None:
    """The correctness metric is only trustworthy if the judge was validated."""
    report = _load_json(JUDGE_VALIDATION_PATH)
    if not report:
        return "judge not validated (run: python -m rag_app.eval.validate_judge)"
    kappa = report.get("cohen_kappa")
    threshold = report.get("threshold", 0.6)
    if kappa is None or kappa < threshold:
        return f"judge validation kappa {kappa} < {threshold}"
    return None


def _check(metrics: Metrics) -> list[str]:
    failures: list[str] = []
    thresholds = _load_json(THRESHOLDS_PATH)
    baseline = _load_json(BASELINE_PATH)
    values = metrics.as_dict()
    for key in _METRIC_KEYS:
        value = float(values[key])
        floor = thresholds.get(key)
        if floor is not None and value < floor:
            failures.append(f"{key}={value:.3f} < threshold {floor:.3f}")
        base = baseline.get(key)
        if base is not None and value < base - _REGRESSION_EPSILON:
            failures.append(f"{key}={value:.3f} regressed below baseline {base:.3f}")
    judge_failure = _judge_validation_failure()
    if judge_failure:
        failures.append(judge_failure)
    return failures


def _load_json_any(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _check_v2(metrics: DialogueMetrics) -> tuple[list[str], list[str]]:
    """Compare golden-set v2 metrics to the plan's floors (``golden_set_v2_floors.json``).

    Returns ``(info_lines, failures)``. A floor only becomes a real FAIL when the floors file's
    ``enforce_now`` is ``true`` — today it is ``false`` (floors apply from 14.2, PHASE_PLANNING
    §14.2 Gate: "floors from 14.0") — so this never fails the eval gate on this branch; every
    comparison is still printed so the "before" gap is visible on every run, not just this one.
    """
    floors = _load_json_any(V2_FLOORS_PATH)
    enforce_now = bool(floors.get("enforce_now", False))
    values = metrics.as_dict()
    info: list[str] = []
    failures: list[str] = []

    def _compare(key: str, *, higher_is_better: bool) -> None:
        value = values.get(key)
        floor = floors.get(key)
        if value is None or floor is None:
            return
        met = float(value) >= float(floor) if higher_is_better else float(value) <= float(floor)
        sign = ">=" if higher_is_better else "<="
        status = "OK" if met else "below floor"
        info.append(f"  {key}: {value:.3f} (floor {sign} {float(floor):.3f}) {status}")
        if enforce_now and not met:
            failures.append(f"{key}={value:.3f} does not meet floor {sign} {float(floor):.3f}")

    for key in _V2_METRIC_KEYS:
        _compare(key, higher_is_better=True)
    for key in _V2_LATENCY_KEYS:
        _compare(key, higher_is_better=False)
    return info, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RAG evaluation gate.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write current metrics to baseline_metrics.json (establish/refresh baseline)",
    )
    parser.add_argument(
        "--require-stack",
        action="store_true",
        help="fail (instead of skipping) when Postgres/Ollama are not reachable (gate --full)",
    )
    parser.add_argument(
        "--update-v2-baseline",
        action="store_true",
        help=(
            "force-rewrite eval/golden_set_v2_baseline.json even if it already exists "
            "(the 'before' baseline is otherwise written once, the first time this file "
            "is missing, and never silently overwritten afterwards)"
        ),
    )
    args = parser.parse_args(argv)

    if not _stack_available():
        if args.require_stack:
            settings = get_settings()
            print(
                "eval gate: FAIL — stack not reachable (Postgres or Ollama at"
                f" {settings.ollama_host}); start it or fix OLLAMA_HOST / DATABASE_URL"
            )
            return 1
        print("eval gate: SKIP (Postgres/Ollama not reachable)")
        return 0

    items = load_golden_set(args.golden)
    session_factory = make_session_factory()
    with session_factory() as session:
        results = evaluate(session, items)
    metrics = compute_metrics(results)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({"metrics": metrics.as_dict(), "items": [vars(r) for r in results]}, indent=2),
        encoding="utf-8",
    )

    print("eval metrics:")
    for key, value in metrics.as_dict().items():
        print(f"  {key}: {value}")

    # Golden set v2 (T14.0.4/T14.0.5): same session/stack, a separate fixture and metrics —
    # never affects the v1 pass/fail below (see the module docstring and _check_v2).
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET_V2)
    with session_factory() as v2_session:
        v2_results = evaluate_dialogues(v2_session, dialogues)
    v2_metrics = compute_dialogue_metrics(v2_results)

    V2_RESULTS_PATH.write_text(
        json.dumps(
            {"metrics": v2_metrics.as_dict(), "turns": [vars(r) for r in v2_results]}, indent=2
        ),
        encoding="utf-8",
    )
    print("\ngolden set v2 metrics (before baseline, floors not yet enforced — see the ADR):")
    for key, value in v2_metrics.as_dict().items():
        print(f"  {key}: {value}")
    v2_info, v2_failures = _check_v2(v2_metrics)
    if v2_info:
        print("vs floors:")
        for line in v2_info:
            print(line)

    # The v2 "before" baseline (T14.0.5) is written once, the first time this file does not
    # exist yet (this run), or any time ``--update-v2-baseline`` is passed explicitly — never
    # silently overwritten on every routine run, same discipline as v1's ``--update-baseline``.
    if not V2_BASELINE_PATH.exists() or args.update_v2_baseline:
        V2_BASELINE_PATH.write_text(json.dumps(v2_metrics.as_dict(), indent=2), encoding="utf-8")
        print(f"golden set v2 baseline written -> {V2_BASELINE_PATH}")

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(metrics.as_dict(), indent=2), encoding="utf-8")
        print(f"baseline updated -> {BASELINE_PATH}")
        return 0

    failures = _check(metrics) + v2_failures
    if failures:
        print("\nEVAL GATE: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nEVAL GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
