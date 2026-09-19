"""Unit tests for eval metrics and judge parsing (no DB/LLM)."""

from __future__ import annotations

from rag_app.eval.judge import parse_verdict
from rag_app.eval.metrics import ItemResult, compute_metrics


def _answerable(expected: str, retrieved: list[str], *, correct: bool) -> ItemResult:
    return ItemResult(
        id="a",
        answerable=True,
        retrieved_docs=retrieved,
        expected_doc=expected,
        abstained=False,
        grounded=True,
        correct=correct,
    )


def _negative(abstained: bool) -> ItemResult:
    return ItemResult(
        id="n",
        answerable=False,
        retrieved_docs=[],
        expected_doc=None,
        abstained=abstained,
        grounded=False,
        correct=None,
    )


def test_compute_metrics_all_good() -> None:
    results = [
        _answerable("doc1", ["doc1", "doc2"], correct=True),
        _answerable("doc3", ["doc3"], correct=True),
        _negative(abstained=True),
    ]
    m = compute_metrics(results)
    assert m.retrieval_recall == 1.0
    assert m.correctness == 1.0
    assert m.faithfulness == 1.0
    assert m.correct_abstention == 1.0
    assert m.n_answerable == 2 and m.n_negative == 1


def test_compute_metrics_penalizes_misses() -> None:
    results = [
        _answerable("doc1", ["doc9"], correct=False),  # retrieval miss + wrong
        _answerable("doc2", ["doc2"], correct=True),
        _negative(abstained=False),  # failed to abstain
    ]
    m = compute_metrics(results)
    assert m.retrieval_recall == 0.5
    assert m.correctness == 0.5
    assert m.correct_abstention == 0.0


def test_abstaining_on_answerable_counts_as_incorrect() -> None:
    item = ItemResult(
        id="a",
        answerable=True,
        retrieved_docs=["doc1"],
        expected_doc="doc1",
        abstained=True,
        grounded=False,
        correct=False,
    )
    m = compute_metrics([item])
    assert m.correctness == 0.0
    # faithfulness has no answered items -> defaults to 1.0 (no penalty)
    assert m.faithfulness == 1.0


def test_parse_verdict() -> None:
    assert parse_verdict("CORRECT") is True
    assert parse_verdict("INCORRECT") is False
    assert parse_verdict("The answer is correct.") is True
    assert parse_verdict("unclear") is False
