"""Unit tests for the agentic router's thin-detection (no DB/LLM)."""

from __future__ import annotations

import pytest

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


# --- T11.4.1: the router defaults to the ONE shared reranker, never a fresh load -----------


def test_answer_agentic_uses_the_shared_reranker_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_app.reranking as reranking
    from rag_app.agentic import answer_agentic

    monkeypatch.setattr(reranking, "_shared_reranker", None)
    shared = reranking.get_shared_reranker()
    seen: list[object] = []

    def fake_retrieve(
        _session: object,
        _query: str,
        *,
        reranker: object,
        candidate_k: int | None = None,
        top_n: int | None = None,
        version: str | None = None,
    ) -> list[RetrievedChunk]:
        seen.append(reranker)
        return [_chunk(0.9)]

    monkeypatch.setattr(reranking, "retrieve", fake_retrieve)

    class _FakeChat:
        def chat(self, _messages: object, **_kwargs: object) -> str:
            return "Use prepared statements [1]."

    answer_agentic(None, "How do I prevent SQL injection?", chat=_FakeChat())  # type: ignore[arg-type]
    assert seen == [shared]
