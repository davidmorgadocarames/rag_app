"""Unit tests for the agentic router's thin-detection (no DB/LLM)."""

from __future__ import annotations

from rag_app.agentic import is_thin
from rag_app.retrieval import RetrievedChunk


def _chunk(score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid="d::0",
        heading="h",
        text="t",
        version="2021",
        effective_date=None,
        score=score,
    )


def test_is_thin_empty() -> None:
    assert is_thin([], 0.5) is True


def test_is_thin_below_threshold() -> None:
    assert is_thin([_chunk(0.3), _chunk(0.1)], 0.5) is True


def test_is_thin_above_threshold() -> None:
    assert is_thin([_chunk(0.9), _chunk(0.2)], 0.5) is False
