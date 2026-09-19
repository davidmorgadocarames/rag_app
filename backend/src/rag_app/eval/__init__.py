"""Evaluation: golden set, metrics, LLM judge, runner, and the deploy gate."""

from rag_app.eval.dataset import GoldenItem, load_golden_set
from rag_app.eval.metrics import ItemResult, Metrics, compute_metrics
from rag_app.eval.runner import evaluate

__all__ = [
    "GoldenItem",
    "ItemResult",
    "Metrics",
    "compute_metrics",
    "evaluate",
    "load_golden_set",
]
