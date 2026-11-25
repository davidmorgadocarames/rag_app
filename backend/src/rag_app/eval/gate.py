"""Evaluation gate: run the eval, enforce thresholds + no-regression, block on failure.

Called by scripts/gate.sh (step ``eval``, part of ``--full`` / ``--only eval``) with
``--require-stack``: an unreachable stack (Postgres + Ollama) then FAILS the gate. Run by
hand without that flag, an unreachable stack is reported as SKIP (exit 0).

**Golden set v2 (T14.0.4/T14.0.5, block 14-A2):** this same step also replays
``eval/golden_set_v2.jsonl`` (dialogues) and records the "before" baseline + the plan's floors
(``eval/golden_set_v2_baseline.json`` / ``eval/golden_set_v2_floors.json``, both git-tracked).
**Per the Phase 14 plan, §14.2's own Gate ("floors from 14.0") the floors are enforced starting at
14.2, once the router/memory they measure exists.** T14.0.5/14-A2/14-C1 kept every floor
record-only (``enforce_now: false``), since none of them were met yet. **T14.2.8 (block 14-C2)
turns enforcement on PER METRIC** (``golden_set_v2_floors.json``'s ``enforce`` object), not with a
single global switch any more: a metric fails the gate only once ITS OWN floor is both (a) in
force per the plan (14.2 onward, which is every floor in this file) and (b) actually met by the
measured numbers -- the plan is read as "apply floors now, enforce the ones that already pass;
do not silently lower a floor just because another metric is still short." See the ADR's
"14-C2 decision" section for which metrics that is, and why.
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
# `router_p95_s` (the metric field, see metrics.py) is checked against the LOCAL floor's own
# key name (`router_p95_s_local`) -- the floors file keeps local/Azure as two separate values
# (P14-D3, the Azure subset is a later block's own eval run) but this gate only ever runs
# against the local stack, so only the local floor applies here.
_V2_ROUTER_LATENCY_FLOOR_KEY = "router_p95_s_local"


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

    Returns ``(info_lines, failures)``. **T14.2.8 (block 14-C2): enforcement is PER METRIC**,
    via the floors file's ``enforce`` object (``{"<metric>": true|false}``) — superseding
    14-A2's single global ``enforce_now`` boolean (kept in the file only as a human-readable
    note of when enforcement started, no longer read by this function). A metric's floor only
    becomes a real FAIL when its OWN ``enforce.<metric>`` is ``true`` AND it is currently below
    its floor; a metric with ``enforce.<metric>: false`` is still PRINTED (so every gap stays
    visible every run) but can never fail the gate. This lets the plan's "floors apply from
    14.2" stay literally true (every floor IS being checked, right now) while a floor this
    block's own measured numbers do not yet meet is recorded, not silently lowered — see the
    ADR's "14-C2 decision" for which floors that is this run.
    """
    floors = _load_json_any(V2_FLOORS_PATH)
    enforce_map: dict[str, Any] = floors.get("enforce", {})
    values = metrics.as_dict()
    info: list[str] = []
    failures: list[str] = []

    def _compare(key: str, floor_key: str | None = None, *, higher_is_better: bool) -> None:
        floor_key = floor_key or key
        value = values.get(key)
        floor = floors.get(floor_key)
        if value is None or floor is None:
            return
        met = float(value) >= float(floor) if higher_is_better else float(value) <= float(floor)
        sign = ">=" if higher_is_better else "<="
        enforced = bool(enforce_map.get(key, False))
        status = "OK" if met else ("FAIL (enforced)" if enforced else "below floor (recorded)")
        info.append(f"  {key}: {value:.3f} (floor {sign} {float(floor):.3f}) {status}")
        if enforced and not met:
            failures.append(f"{key}={value:.3f} does not meet floor {sign} {float(floor):.3f}")

    for key in _V2_METRIC_KEYS:
        _compare(key, higher_is_better=True)
    for key in _V2_LATENCY_KEYS:
        _compare(key, higher_is_better=False)
    _compare("router_p95_s", _V2_ROUTER_LATENCY_FLOOR_KEY, higher_is_better=False)
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
    print("\ngolden set v2 metrics (per-metric floor enforcement -- see the ADR):")
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
