"""API tests with stubbed dependencies (no DB/LLM needed)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from rag_app.api.app import create_app
from rag_app.api.auth import get_current_user
from rag_app.api.deps import AnswerFn, get_answer_reserver, get_answerer, get_session
from rag_app.generation import Answer, Citation


def test_health() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _stub_answerer() -> AnswerFn:
    def _answer(session: object, question: str, version: str | None) -> Answer:
        return Answer(
            text="Use prepared statements [1].",
            citations=[
                Citation(
                    marker=1,
                    chunk_uid="cs-sql-injection-prevention::0",
                    heading="SQL Injection Prevention",
                    version="current",
                    effective_date=None,
                )
            ],
            abstained=False,
            grounded=True,
        )

    return _answer


def test_chat_stubbed() -> None:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_answerer] = _stub_answerer
    app.dependency_overrides[get_current_user] = lambda: object()
    # The daily cap needs the database: covered in test_daily_cap.
    app.dependency_overrides[get_answer_reserver] = lambda: lambda _session: None
    client = TestClient(app)

    response = client.post("/chat", json={"question": "How do I prevent SQL injection?"})
    assert response.status_code == 200
    body = response.json()
    assert "prepared statements" in body["answer"]
    assert body["grounded"] is True
    assert body["citations"][0]["marker"] == 1
    assert body["citations"][0]["version"] == "current"


def test_chat_requires_authentication() -> None:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_answerer] = _stub_answerer
    client = TestClient(app)

    response = client.post("/chat", json={"question": "How do I prevent SQL injection?"})
    assert response.status_code == 401


def test_chat_validation_rejects_empty_question() -> None:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: object()
    client = TestClient(app)
    response = client.post("/chat", json={"question": ""})
    assert response.status_code == 422


# --- T11.4.1: single shared reranker, end to end through the real dependency ---------------


def _fake_chunk() -> Any:
    from rag_app.retrieval import RetrievedChunk

    return RetrievedChunk(
        chunk_uid="cs-sql::0",
        heading="SQL Injection Prevention",
        text="Use prepared statements.",
        version="current",
        effective_date=None,
        score=1.0,
    )


def test_chat_reuses_the_shared_reranker_across_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Done-when (T11.4.1): N real ``/chat`` requests through the REAL (not stubbed)
    ``get_answerer()`` dependency construct the cross-encoder model only once — the
    constructor call count is the proof, same guarantee as ``reranking.get_shared_reranker``'s
    own unit test, exercised here through the actual API wiring (lifespan is not run by this
    ``TestClient``, so the model is still cold before the first request; that is fine — the
    lifespan's warm-up is proven separately in test_api_lifespan.py)."""
    from rag_app import generation, reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)
    # answer_question's rerank path calls reranking.retrieve -> reranking.hybrid_search (its
    # OWN imported reference), not generation.hybrid_search (only the no-rerank path there).
    monkeypatch.setattr(reranking, "hybrid_search", lambda *_a, **_k: [_fake_chunk()])

    construct_calls: list[tuple[Any, ...]] = []

    class _FakeCrossEncoder:
        def __init__(self, *args: object, **kwargs: object) -> None:
            construct_calls.append((args, kwargs))

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [1.0 for _ in pairs]

    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _FakeCrossEncoder())

    class _FakeChat:
        def chat(self, _messages: object, **_kwargs: object) -> str:
            return "Use prepared statements [1]."

    monkeypatch.setattr(generation, "make_chat_client", lambda *_a, **_k: _FakeChat())

    app = create_app()
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: object()
    app.dependency_overrides[get_answer_reserver] = lambda: lambda _session: None
    client = TestClient(app)

    for _ in range(3):
        response = client.post("/chat", json={"question": "How do I prevent SQL injection?"})
        assert response.status_code == 200

    assert len(construct_calls) == 1, f"expected ONE model load, got {len(construct_calls)}"
