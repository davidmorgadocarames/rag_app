"""Evaluation gate: run the eval, enforce thresholds + no-regression, block on failure.

Called by scripts/gate.sh (step 11) and thus by the pre-push hook. If the eval stack
(Postgres + Ollama) is not reachable, it SKIPS (exit 0) rather than failing, so
doc-only pushes are not blocked; the authoritative gate runs when the stack is up.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
from sqlalchemy import text

from rag_app.config import get_settings
from rag_app.db.session import make_engine, make_session_factory
from rag_app.eval.dataset import DEFAULT_GOLDEN_SET, load_golden_set
from rag_app.eval.metrics import Metrics, compute_metrics
from rag_app.eval.runner import evaluate

REPO_ROOT = Path(__file__).resolve().parents[4]
EVAL_DIR = REPO_ROOT / "eval"
THRESHOLDS_PATH = EVAL_DIR / "thresholds.json"
BASELINE_PATH = EVAL_DIR / "baseline_metrics.json"
RESULTS_PATH = EVAL_DIR / "results.json"

_REGRESSION_EPSILON = 0.02
_METRIC_KEYS = ("retrieval_recall", "faithfulness", "correctness", "correct_abstention")


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
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RAG evaluation gate.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write current metrics to baseline_metrics.json (establish/refresh baseline)",
    )
    args = parser.parse_args(argv)

    if not _stack_available():
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

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(metrics.as_dict(), indent=2), encoding="utf-8")
        print(f"baseline updated -> {BASELINE_PATH}")
        return 0

    failures = _check(metrics)
    if failures:
        print("\nEVAL GATE: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nEVAL GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
