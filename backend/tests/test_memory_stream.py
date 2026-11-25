"""Conversation memory (T14.2.5/T14.2.6, block 14-C1) through the REAL ``/chat/stream``
endpoint, on the harness database (marker ``db``).

Same fixtures/helper style as ``test_daily_cap.py`` (``_FakePipeline`` stands in for
``answer_question_stream`` so these tests never need a live Ollama): ``OLLAMA_HOST`` points
at a closed port, so the memory-WRITE call (``assistant.memory.summarize_turn``, via a real
``make_chat_client()``) always fails fast with a connection error and is silently dropped
(the stream must complete successfully regardless) unless a test explicitly monkeypatches
``conversations.summarize_turn`` itself to control what memory write "succeeds" with.
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

from rag_app.crypto import decrypt, encrypt

QUESTION = "How do I prevent SQL injection?"


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")  # any real LLM call fails fast
    monkeypatch.setenv("DAILY_ANSWER_CAP", "1000")


@pytest.fixture()
def client(env: None, db_engine: Engine) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.api.deps import get_rate_limiter
    from rag_app.ratelimit import RateLimiter

    app = create_app()
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(100_000, 1_000.0)
    yield TestClient(app)


def _make_user_with_key(engine: Engine) -> tuple[uuid.UUID, bytes]:
    """A user plus the RAW per-user data key (unlike the other test files' ``_make_user``,
    which never needs it) -- so a test can seed/verify ciphertext the same way the real
    endpoint would."""
    from rag_app.crypto import generate_user_key, wrap_key
    from rag_app.db.models import User, UserKey
    from rag_app.security import hash_password

    data_key = generate_user_key()
    with Session(engine) as session:
        user = User(
            email=f"memory-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password("correct horse battery"),
        )
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(data_key)))
        session.commit()
        return user.id, data_key


def _make_conversation_with_turn(
    engine: Engine,
    user_id: uuid.UUID,
    key: bytes,
    *,
    question: str,
    answer: str,
    summary: str | None = None,
) -> str:
    """A conversation that already has one clean user+assistant turn (and optionally a
    summary) -- the state :func:`rag_app.api.conversations._load_memory` must read BEFORE a
    second message is posted to it."""
    from rag_app.db.models import Conversation, Message

    with Session(engine) as session:
        conv = Conversation(
            user_id=user_id,
            summary_encrypted=encrypt(key, summary) if summary is not None else None,
        )
        session.add(conv)
        session.flush()
        session.add(
            Message(
                conversation_id=conv.id,
                role="user",
                content_encrypted=encrypt(key, json.dumps({"text": question})),
            )
        )
        session.add(
            Message(
                conversation_id=conv.id,
                role="assistant",
                content_encrypted=encrypt(key, json.dumps({"text": answer})),
            )
        )
        session.commit()
        return str(conv.id)


def _auth(engine: Engine, user_id: uuid.UUID) -> dict[str, str]:
    from conftest import cookie_auth_headers

    return cookie_auth_headers(engine, user_id)


def _events(res: Any) -> list[dict]:
    assert res.status_code == 200
    return [
        json.loads(frame[len("data: ") :])
        for frame in res.text.split("\n\n")
        if frame.startswith("data: ")
    ]


def _summary_encrypted(engine: Engine, conv_id: str) -> bytes | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT summary_encrypted FROM conversations WHERE id = :c"), {"c": conv_id}
        ).scalar_one()


class _FakePipeline:
    """Stands in for ``answer_question_stream`` (same shape as ``test_daily_cap.py``'s own):
    records the ``summary``/``last_turn`` kwargs it was called with."""

    def __init__(self, *, answer_text: str = "Use prepared statements [1].") -> None:
        self.calls: list[dict[str, Any]] = []
        self._answer_text = answer_text

    def __call__(
        self,
        _session: Any,
        _question: str,
        *,
        summary: str | None = None,
        last_turn: str | None = None,
        **_kw: Any,
    ) -> Iterator[Any]:
        from rag_app.generation import Answer, StreamResult, StreamStage
        from rag_app.llm import Usage

        self.calls.append({"summary": summary, "last_turn": last_turn})
        yield StreamStage("generating")
        yield StreamResult(Answer(text=self._answer_text), usage=Usage(10, 5))


# --- T14.2.5: memory read is scoped to exactly ONE conversation -----------------------------


@pytest.mark.db
def test_stream_reads_this_conversations_own_memory_and_never_another_one(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two conversations of the SAME user, each with its own distinct prior turn and
    summary: posting a follow-up to conversation A must see ONLY A's memory, and a follow-up
    to B must see ONLY B's -- never the other's (the authorization property T14.2.5 asks
    for, demonstrated directly rather than only via the pre-existing cross-USER 404 check)."""
    from rag_app.api import conversations

    user, key = _make_user_with_key(db_engine)
    conv_a = _make_conversation_with_turn(
        db_engine,
        user,
        key,
        question="How do I prevent SQL injection?",
        answer="Use prepared statements.",
        summary='{"topics": ["sql injection"]}',
    )
    conv_b = _make_conversation_with_turn(
        db_engine,
        user,
        key,
        question="What is broken access control?",
        answer="Enforce authorization server-side.",
        summary='{"topics": ["access control"]}',
    )

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    monkeypatch.setattr(conversations, "summarize_turn", lambda *_a, **_k: None)

    res_a = client.post(
        "/chat/stream",
        json={"question": "and OS commands?", "conversation_id": conv_a},
        headers=_auth(db_engine, user),
    )
    res_b = client.post(
        "/chat/stream",
        json={"question": "and CSRF?", "conversation_id": conv_b},
        headers=_auth(db_engine, user),
    )
    assert _events(res_a)[-1]["type"] == "done"
    assert _events(res_b)[-1]["type"] == "done"

    assert len(pipeline.calls) == 2
    call_a, call_b = pipeline.calls
    assert call_a["summary"] == '{"topics": ["sql injection"]}'
    assert call_a["last_turn"] == (
        "User: How do I prevent SQL injection?\nAssistant: Use prepared statements."
    )
    assert call_b["summary"] == '{"topics": ["access control"]}'
    assert call_b["last_turn"] == (
        "User: What is broken access control?\nAssistant: Enforce authorization server-side."
    )
    # Neither call ever saw the other conversation's memory.
    assert "access control" not in call_a["summary"]
    assert "sql injection" not in call_b["summary"]
    assert "broken access control" not in call_a["last_turn"]
    assert "SQL injection" not in call_b["last_turn"]


@pytest.mark.db
def test_stream_has_no_memory_for_a_brand_new_conversation(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_app.api import conversations

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    monkeypatch.setattr(conversations, "summarize_turn", lambda *_a, **_k: None)
    user = _make_user_with_key(db_engine)[0]

    res = client.post("/chat/stream", json={"question": QUESTION}, headers=_auth(db_engine, user))

    assert _events(res)[-1]["type"] == "done"
    assert pipeline.calls == [{"summary": None, "last_turn": None}]


# --- T14.2.6: memory write happens after the answer, before "done" --------------------------


@pytest.mark.db
def test_stream_writes_the_updated_summary_before_the_done_event(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_app.api import conversations

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    monkeypatch.setattr(
        conversations, "summarize_turn", lambda *_a, **_k: '{"topics": ["sql injection"]}'
    )
    user, key = _make_user_with_key(db_engine)

    res = client.post("/chat/stream", json={"question": QUESTION}, headers=_auth(db_engine, user))
    events = _events(res)
    assert events[-1]["type"] == "done"
    conv_id = events[0]["conversation_id"]

    blob = _summary_encrypted(db_engine, conv_id)
    assert blob is not None
    assert decrypt(key, blob) == '{"topics": ["sql injection"]}'


@pytest.mark.db
def test_stream_leaves_the_summary_unchanged_when_the_update_is_dropped(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``summarize_turn`` returning ``None`` (invalid/unreachable, same contract as the real
    function) must leave whatever summary already existed exactly as it was -- never erase
    it, never write a placeholder."""
    from rag_app.api import conversations

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    monkeypatch.setattr(conversations, "summarize_turn", lambda *_a, **_k: None)
    user, key = _make_user_with_key(db_engine)
    conv_id = _make_conversation_with_turn(
        db_engine,
        user,
        key,
        question="How do I prevent SQL injection?",
        answer="Use prepared statements.",
        summary='{"topics": ["sql injection"]}',
    )

    res = client.post(
        "/chat/stream",
        json={"question": "and OS commands?", "conversation_id": conv_id},
        headers=_auth(db_engine, user),
    )
    assert _events(res)[-1]["type"] == "done"

    blob = _summary_encrypted(db_engine, conv_id)
    assert blob is not None
    assert decrypt(key, blob) == '{"topics": ["sql injection"]}'  # unchanged


@pytest.mark.db
def test_stream_never_fails_when_the_memory_write_call_is_unreachable(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No monkeypatch of ``summarize_turn`` here: the REAL function runs, calls a REAL
    ``make_chat_client()`` against ``OLLAMA_HOST`` (a closed port, see the ``env`` fixture) --
    a real connection error. The turn must still complete successfully; memory is
    best-effort (T14.2.6's own contract)."""
    from rag_app.api import conversations

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    user = _make_user_with_key(db_engine)[0]

    res = client.post("/chat/stream", json={"question": QUESTION}, headers=_auth(db_engine, user))
    events = _events(res)
    assert events[-1]["type"] == "done"
    conv_id = events[0]["conversation_id"]
    assert _summary_encrypted(db_engine, conv_id) is None


# --- DA-14-C1 finding 1 fix (14-C1-fix): no second, uncounted LLM call on a fast-path turn --


class _CountingChatClient:
    """Every real LLM call across the whole turn, shared between the REAL pipeline
    (``generation.make_chat_client``) and the memory write (``conversations.make_chat_client``)
    -- the total is the true per-turn LLM-call count, not a per-module guess. Drives the REAL
    ``answer_question_stream``/``understand()``/``generate_chitchat_reply`` (no ``_FakePipeline``
    here), because the bug this fixes is specifically about how many real LLM calls one turn
    costs."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, _messages: Any, **_kw: Any) -> str:
        self.calls += 1
        return "Hi there!"

    def chat_stream(self, _messages: Any, **_kw: Any) -> Iterator[str]:
        self.calls += 1
        yield "Hi there!"

    def chat_json(self, _messages: Any, _schema: Any, *, fallback: Any, **_kw: Any) -> Any:
        self.calls += 1
        return fallback


@pytest.mark.db
def test_stream_makes_exactly_one_llm_call_on_an_exact_fast_path_turn(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-14-C1 finding 1 (HIGH): an exact greeting matches ``match_fast_path``, which
    already exempts the turn from the daily cap (14-B2-fix) -- it must not ALSO cost a
    second, uncounted LLM call for the memory write. Fails on 9b6bb2e: the real
    ``_update_conversation_summary`` call there is unconditional, so ``chat.calls`` is 2
    (the chit-chat reply + ``summarize_turn``'s ``chat_json``) and the summary is written."""
    from rag_app import generation
    from rag_app.api import conversations

    chat = _CountingChatClient()
    monkeypatch.setattr(generation, "make_chat_client", lambda *_a, **_kw: chat)
    monkeypatch.setattr(conversations, "make_chat_client", lambda *_a, **_kw: chat)
    user = _make_user_with_key(db_engine)[0]

    res = client.post("/chat/stream", json={"question": "hola"}, headers=_auth(db_engine, user))
    events = _events(res)
    assert events[-1]["type"] == "done"
    conv_id = events[0]["conversation_id"]

    assert chat.calls == 1  # the chit-chat reply only
    assert _summary_encrypted(db_engine, conv_id) is None
