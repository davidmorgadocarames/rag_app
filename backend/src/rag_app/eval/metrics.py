"""Evaluation metrics — pure aggregation over per-item results.

Retrieval and generation are measured separately, plus correctness against an
independent ground truth (catches the *faithful-but-stale* failure), plus correct
abstention on out-of-corpus questions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ItemResult:
    id: str
    answerable: bool
    retrieved_docs: list[str]
    expected_doc: str | None
    abstained: bool
    grounded: bool
    correct: bool | None  # judge verdict for answerable items; None for negatives


@dataclass
class Metrics:
    n_answerable: int
    n_negative: int
    retrieval_recall: float
    faithfulness: float
    correctness: float
    correct_abstention: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def compute_metrics(results: list[ItemResult]) -> Metrics:
    """Aggregate per-item results into the gate metrics."""
    answerable = [r for r in results if r.answerable]
    negative = [r for r in results if not r.answerable]

    recall_hits = sum(
        1 for r in answerable if r.expected_doc and r.expected_doc in r.retrieved_docs
    )
    answered = [r for r in answerable if not r.abstained]
    faithful_hits = sum(1 for r in answered if r.grounded)
    correct_hits = sum(1 for r in answerable if r.correct is True)
    abstained_hits = sum(1 for r in negative if r.abstained)

    return Metrics(
        n_answerable=len(answerable),
        n_negative=len(negative),
        retrieval_recall=_ratio(recall_hits, len(answerable)),
        faithfulness=_ratio(faithful_hits, len(answered)),
        correctness=_ratio(correct_hits, len(answerable)),
        correct_abstention=_ratio(abstained_hits, len(negative)),
    )


def cohen_kappa(y_true: list[bool], y_pred: list[bool]) -> float:
    """Cohen's kappa: agreement corrected for chance (used to validate the LLM judge)."""
    n = len(y_true)
    if n == 0:
        return 1.0
    observed = sum(1 for a, b in zip(y_true, y_pred, strict=True) if a == b) / n
    expected = 0.0
    for label in (True, False):
        p_true = sum(1 for a in y_true if a == label) / n
        p_pred = sum(1 for b in y_pred if b == label) / n
        expected += p_true * p_pred
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)
