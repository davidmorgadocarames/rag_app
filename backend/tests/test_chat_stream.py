"""``/chat/stream`` error handling and orphan conversations (T11.2.16, F-2026-09-27-4/5).

On the harness database, through the real API and the real pipeline code; the failures are
real ones where possible (Ollama pointed at a closed port → ``ConnectError`` in the embedder
or the LLM client; a data key wrapped with another master key → ``InvalidToken``):

- the first SSE event carries ``conversation_id``;
- every failure ends the stream with one ``error`` event carrying the id (never a cut
  stream), and the failed turn is stored as an assistant error marker;
- the next message with that id reuses the conversation;
- no conversation ever holds a user message without a reply (no orphans);
- ``GET /conversations`` never 500s because of one bad row or an unreadable data key.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.db

CLOSED_PORT = "http://127.0.0.1:9"  # nothing listens there: a real connection error


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATA_MASTER_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("OLLAMA_HOST", CLOSED_PORT)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    return key


@pytest.fixture()
def client(env: str) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.api.deps import get_rate_limiter
    from rag_app.ratelimit import RateLimiter

    app = create_app()
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(100_000, 1_000.0)
    # No lifespan: each test uses its own throwaway master key (fingerprint not under test).
    yield TestClient(app)


def _make_user(engine: Engine, *, foreign_key: bool = False) -> uuid.UUID:
    """A user; ``foreign_key`` wraps the data key with ANOTHER master key (the F-4 case)."""
    from rag_app.crypto import generate_user_key, wrap_key
    from rag_app.db.models import User, UserKey
    from rag_app.security import hash_password

    if foreign_key:
        wrapped = Fernet(Fernet.generate_key()).encrypt(generate_user_key())
    else:
        wrapped = wrap_key(generate_user_key())
    with Session(engine) as session:
        user = User(
            email=f"stream-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password("correct horse battery"),
        )
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrapped))
        session.commit()
        return user.id


def _auth(user_id: uuid.UUID) -> dict[str, str]:
    from rag_app.security import create_token

    return {"Authorization": f"Bearer {create_token(str(user_id))}"}


def _stream(client: Any, user_id: uuid.UUID, question: str, conv: str | None = None) -> list[dict]:
    body: dict[str, Any] = {"question": question}
    if conv:
        body["conversation_id"] = conv
    res = client.post("/chat/stream", json=body, headers=_auth(user_id))
    assert res.status_code == 200
    events = [
        json.loads(frame[len("data: ") :])
        for frame in res.text.split("\n\n")
        if frame.startswith("data: ")
    ]
    assert events, "the stream is never empty"
    return events


def _turns(engine: Engine, user_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    """{conversation id: (user messages, assistant messages)}."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.id::text,"
                " count(*) FILTER (WHERE m.role = 'user'),"
                " count(*) FILTER (WHERE m.role = 'assistant')"
                " FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id"
                " WHERE c.user_id = :u GROUP BY c.id"
            ),
            {"u": user_id},
        ).all()
    return {cid: (int(u), int(a)) for cid, u, a in rows}


def _assert_no_orphans(engine: Engine, user_id: uuid.UUID) -> None:
    for cid, (users, assistants) in _turns(engine, user_id).items():
        assert users >= 1 and users == assistants, f"orphan turn in {cid}: {users}/{assistants}"


def _assert_error_contract(events: list[dict], code: str) -> str | None:
    assert events[0]["type"] == "conversation"
    conv_id = events[0]["conversation_id"]
    last = events[-1]
    assert last["type"] == "error" and last["code"] == code
    assert last["conversation_id"] == conv_id and last["detail"]
    assert [e["type"] for e in events].count("error") == 1
    assert not any(e["type"] == "done" for e in events)
    return conv_id


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


# --- failures -------------------------------------------------------------------------------


def test_an_embedding_failure_ends_with_an_error_event_and_the_next_message_reuses_it(
    client: Any, db_engine: Engine
) -> None:
    user = _make_user(db_engine)
    events = _stream(client, user, "How do I prevent SQL injection?")
    conv = _assert_error_contract(events, "retrieval_failed")
    assert conv is not None
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["classifying", "retrieving"]
    assert _turns(db_engine, user) == {conv: (1, 1)}  # user message + error marker

    # the next message (chitchat: no Ollama needed) goes into the same conversation
    events = _stream(client, user, "hello", conv)
    assert events[0] == {"type": "conversation", "conversation_id": conv}
    assert events[-1]["type"] == "done" and events[-1]["conversation_id"] == conv
    assert _turns(db_engine, user) == {conv: (2, 2)}

    detail = client.get(f"/conversations/{conv}", headers=_auth(user)).json()
    assert [(m["role"], m["error"]) for m in detail["messages"]] == [
        ("user", False),
        ("assistant", True),
        ("user", False),
        ("assistant", False),
    ]
    assert "search service" in detail["messages"][1]["content"]
    _assert_no_orphans(db_engine, user)


def test_an_llm_failure_ends_with_an_error_event(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_app import generation, reranking

    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_fake_chunk()])
    monkeypatch.setattr(
        reranking.CrossEncoderReranker, "rerank", lambda _self, _q, chunks, _n: chunks
    )
    user = _make_user(db_engine)
    events = _stream(client, user, "How do I prevent SQL injection?")
    conv = _assert_error_contract(events, "generation_failed")
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages == ["classifying", "retrieving", "reranking", "generating"]
    assert conv is not None and _turns(db_engine, user) == {conv: (1, 1)}

    # the id is kept: a second failing message stays in the same conversation
    events = _stream(client, user, "And XSS?", conv)
    assert _assert_error_contract(events, "generation_failed") == conv
    assert _turns(db_engine, user) == {conv: (2, 2)}
    _assert_no_orphans(db_engine, user)


def test_an_unwrappable_data_key_is_an_error_event_and_creates_nothing(
    client: Any, db_engine: Engine
) -> None:
    user = _make_user(db_engine, foreign_key=True)
    events = _stream(client, user, "hello")
    assert _assert_error_contract(events, "key_unavailable") is None
    assert _turns(db_engine, user) == {}  # nothing could be encrypted: nothing created

    # an existing conversation: the error carries its id, nothing is appended to it
    with Session(db_engine) as session:
        from rag_app.db.models import Conversation

        existing = Conversation(user_id=user)
        session.add(existing)
        session.commit()
        conv = str(existing.id)
    events = _stream(client, user, "hello", conv)
    assert _assert_error_contract(events, "key_unavailable") == conv
    assert _turns(db_engine, user) == {conv: (0, 0)}

    # the list does not 500: every row is listed as unreadable; the detail is a clean 409
    res = client.get("/conversations", headers=_auth(user))
    assert res.status_code == 200
    assert [(c["id"], c["unreadable"]) for c in res.json()] == [(conv, True)]
    assert client.get(f"/conversations/{conv}", headers=_auth(user)).status_code == 409


def test_a_storage_failure_is_an_error_event(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    from rag_app.api import conversations

    def _down(*_a: object, **_k: object) -> None:
        raise OperationalError("INSERT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(conversations, "_start_turn", _down)
    user = _make_user(db_engine)
    events = _stream(client, user, "hello")
    assert _assert_error_contract(events, "storage_failed") is None
    assert _turns(db_engine, user) == {}


def test_someone_elses_conversation_is_a_404_before_streaming(
    client: Any, db_engine: Engine
) -> None:
    owner, other = _make_user(db_engine), _make_user(db_engine)
    conv = _stream(client, owner, "hello")[0]["conversation_id"]
    res = client.post(
        "/chat/stream", json={"question": "hello", "conversation_id": conv}, headers=_auth(other)
    )
    assert res.status_code == 404
    assert _turns(db_engine, other) == {}
    assert _turns(db_engine, owner) == {conv: (1, 1)}


# --- listing never 500s on one bad row -----------------------------------------------------


def test_one_undecryptable_row_does_not_break_the_list(client: Any, db_engine: Engine) -> None:
    user = _make_user(db_engine)
    good = _stream(client, user, "hello")[0]["conversation_id"]
    bad = _stream(client, user, "hi")[0]["conversation_id"]
    with db_engine.begin() as conn:  # damage the first user message of one conversation
        conn.execute(
            text("UPDATE messages SET content_encrypted = :junk WHERE conversation_id = :c"),
            {"junk": b"not a fernet token", "c": bad},
        )
    res = client.get("/conversations", headers=_auth(user))
    assert res.status_code == 200
    by_id = {c["id"]: c for c in res.json()}
    assert by_id[good]["unreadable"] is False and by_id[good]["title"] == "hello"
    assert by_id[bad]["unreadable"] is True and by_id[bad]["title"] == "Unreadable conversation"

    detail = client.get(f"/conversations/{bad}", headers=_auth(user))
    assert detail.status_code == 200
    assert all(m["error"] for m in detail.json()["messages"])
    _assert_no_orphans(db_engine, user)
