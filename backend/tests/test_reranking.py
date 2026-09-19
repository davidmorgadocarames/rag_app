"""Unit tests for rerank ordering (no torch/model required)."""

from __future__ import annotations

import pytest

from rag_app.reranking import order_by_scores
from rag_app.retrieval import RetrievedChunk


def _chunk(uid: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid=uid,
        heading="",
        text=f"text {uid}",
        version="2021",
        effective_date=None,
        score=0.0,
    )


def test_order_by_scores_sorts_desc_and_truncates() -> None:
    candidates = [_chunk("a"), _chunk("b"), _chunk("c")]
    ranked = order_by_scores(candidates, [0.1, 0.9, 0.5], top_n=2)
    assert [c.chunk_uid for c in ranked] == ["b", "c"]
    assert ranked[0].score == 0.9


def test_order_by_scores_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="same length"):
        order_by_scores([_chunk("a")], [0.1, 0.2], top_n=1)


def test_order_by_scores_top_n_larger_than_input() -> None:
    ranked = order_by_scores([_chunk("a")], [0.3], top_n=10)
    assert len(ranked) == 1
