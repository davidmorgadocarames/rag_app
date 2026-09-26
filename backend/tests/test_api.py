"""API tests with stubbed dependencies (no DB/LLM needed)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_app.api.app import create_app
from rag_app.api.auth import get_current_user
from rag_app.api.deps import AnswerFn, get_answerer, get_session
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
