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
import logging
import threading
import time
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


# --- connections, keep-alive, client disconnect (DA-G2-2, DA-G2-3) -------------------------


def _idle_in_transaction(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
        )


def test_no_connection_sits_idle_in_transaction_during_a_stream(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-G2-2: while the answer is being generated, neither the request session (FastAPI
    >= 0.118 closes `yield` dependencies only after the response) nor the pipeline's session
    holds a connection idle in transaction."""
    from rag_app.api import conversations
    from rag_app.generation import StreamStage

    started, release = threading.Event(), threading.Event()

    def slow_pipeline(session: Session, _question: str, **_kw: Any) -> Iterator[Any]:
        session.execute(text("SELECT 1"))  # the pipeline reads (retrieval), then generates
        yield StreamStage("generating")
        started.set()
        release.wait(timeout=15)
        raise RuntimeError("the model went away")

    monkeypatch.setattr(conversations, "answer_question_stream", slow_pipeline)
    user = _make_user(db_engine)
    outcome: dict[str, list[dict]] = {}
    call = threading.Thread(target=lambda: outcome.update(e=_stream(client, user, "hello?")))
    call.start()
    try:
        assert started.wait(timeout=15), "the stream never reached the generating stage"
        time.sleep(0.3)
        held = _idle_in_transaction(db_engine)
    finally:
        release.set()
        call.join(timeout=30)
    assert held == 0, f"{held} connection(s) idle in transaction during the stream"
    _assert_error_contract(outcome["e"], "generation_failed")


def test_a_long_stage_sends_keep_alive_comments(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-G2-3: a stage that takes long (reranker download at a cold start) still sends
    bytes — an SSE comment every KEEPALIVE_SECONDS — so no proxy cuts the idle stream."""
    from rag_app.api import conversations
    from rag_app.generation import StreamStage

    def slow_pipeline(_session: Session, _question: str, **_kw: Any) -> Iterator[Any]:
        yield StreamStage("reranking")
        time.sleep(0.5)
        raise RuntimeError("reranker download failed")

    monkeypatch.setattr(conversations, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(conversations, "answer_question_stream", slow_pipeline)
    user = _make_user(db_engine)
    res = client.post("/chat/stream", json={"question": "hi?"}, headers=_auth(user))
    assert res.status_code == 200
    frames = res.text.split("\n\n")
    assert frames.count(": keep-alive") >= 3
    parsed = [json.loads(f[len("data: ") :]) for f in frames if f.startswith("data: ")]
    _assert_error_contract(parsed, "retrieval_failed")
    _assert_no_orphans(db_engine, user)


def _interrupted_marker(engine: Engine, user_id: uuid.UUID) -> list[dict]:
    from rag_app.crypto import decrypt, unwrap_key

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.content_encrypted FROM messages m JOIN conversations c"
                " ON c.id = m.conversation_id WHERE c.user_id = :u AND m.role = 'assistant'"
            ),
            {"u": user_id},
        ).all()
        wrapped = conn.execute(
            text("SELECT wrapped_key FROM user_keys WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
    data_key = unwrap_key(bytes(wrapped))
    return [json.loads(decrypt(data_key, bytes(r[0]))) for r in rows]


def test_a_client_that_goes_away_leaves_an_interrupted_marker(
    env: str, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-G2-3: the stream generator closed after the first event (what Starlette does when
    the client disconnects) still answers the stored user message: no orphan turn."""
    from rag_app.api import conversations
    from rag_app.api.schemas import ChatStreamRequest
    from rag_app.generation import StreamStage

    def slow_pipeline(_session: Session, _question: str, **_kw: Any) -> Iterator[Any]:
        yield StreamStage("retrieving")
        time.sleep(5)

    monkeypatch.setattr(conversations, "answer_question_stream", slow_pipeline)
    user = _make_user(db_engine)
    with db_engine.connect() as conn:
        wrapped = conn.execute(
            text("SELECT wrapped_key FROM user_keys WHERE user_id = :u"), {"u": user}
        ).scalar_one()
    stream = conversations._chat_events(user, bytes(wrapped), ChatStreamRequest(question="hi?"))
    first = json.loads(next(stream)[len("data: ") :])
    assert first["type"] == "conversation" and first["conversation_id"]
    stream.close()  # the client went away
    assert _turns(db_engine, user) == {first["conversation_id"]: (1, 1)}
    _assert_no_orphans(db_engine, user)
    (marker,) = _interrupted_marker(db_engine, user)
    assert marker["error"] is True and marker["error_code"] == "interrupted"


def test_a_real_disconnect_leaves_an_interrupted_marker(
    env: str, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-G2-3 end to end: a real uvicorn server, a real socket closed after the first event,
    and the keep-alive that makes the server notice."""
    import socket

    from rag_app.api import conversations
    from rag_app.api.app import create_app
    from rag_app.generation import StreamStage
    from test_log_hygiene import _Server

    def slow_pipeline(_session: Session, _question: str, **_kw: Any) -> Iterator[Any]:
        yield StreamStage("retrieving")
        time.sleep(3)
        raise RuntimeError("never reached by the client")

    monkeypatch.setattr(conversations, "KEEPALIVE_SECONDS", 0.2)
    monkeypatch.setattr(conversations, "answer_question_stream", slow_pipeline)
    user = _make_user(db_engine)
    body = json.dumps({"question": "hi?"}).encode()
    token = _auth(user)["Authorization"]
    with _Server(create_app) as server:
        host, port = server.base.removeprefix("http://").split(":")
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall(
            b"POST /chat/stream HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
            + f"Authorization: {token}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        received = b""
        while b'"type": "conversation"' not in received:
            chunk = sock.recv(4096)
            assert chunk, "the server closed the stream early"
            received += chunk
        sock.close()  # the tab is closed
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            turns = _turns(db_engine, user)
            if turns and all(a == 1 for _u, a in turns.values()):
                break
            time.sleep(0.1)
    _assert_no_orphans(db_engine, user)
    (marker,) = _interrupted_marker(db_engine, user)
    assert marker["error_code"] == "interrupted"


# --- DA-11bA-1: timing.recorder() across the real SSE/threadpool boundary -------------------


def test_a_real_streamed_answer_logs_every_timing_stage_through_the_threadpool(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """DA-11bA-1 (block A review, T11.3.1): a `contextvars.ContextVar` set inside
    `timing.recorder()` and consumed one-`next()`-per-SSE-chunk via Starlette's
    `iterate_in_threadpool` (each `next()` dispatched through a FRESH
    `anyio.to_thread.run_sync`/`copy_context()`) could in principle lose its value, or raise
    on `_current.reset(token)` across that boundary — the DA reproduced exactly that failure
    in an isolated toy repro, but could not reproduce it against the real app. This test
    protects that currently-correct behaviour against a future anyio/Starlette upgrade: a
    real, non-trivial (not chit-chat) `/chat/stream` answer through `TestClient` -> ASGI ->
    the real worker-thread dispatch in `_pipeline_events`, mocking only the external model
    calls (Ollama embed/generate, the reranker's model load) — real Postgres hybrid search,
    the reranker's own `rerank_load`/`rerank_inference` timing wrap and the real SSE/
    threadpool plumbing all run unmocked. Every stage key must show up in the ONE emitted
    `answer_timing` JSON record (not just `ttft_ms`/`total_ms`, which a chit-chat-only test
    would incidentally cover)."""
    from rag_app import embeddings, generation, reranking
    from rag_app.db.models import Chunk, Document
    from rag_app.llm import Usage

    doc = Document(slug="sqli-cs-da11ba1", version="current")
    with Session(db_engine) as session:
        session.add(doc)
        session.flush()
        session.add(
            Chunk(
                document_id=doc.id,
                chunk_uid="sqli-cs-da11ba1::0",
                heading="SQL Injection Prevention",
                ordinal=0,
                text="Use parameterized queries (prepared statements) to prevent SQL injection.",
                embedding=[0.1] * 1024,
                version="current",
            )
        )
        session.commit()

    # Mock only the external model calls (T11.3.1 review note): the embedder's HTTP call to
    # Ollama and the reranker's (heavy, downloaded) cross-encoder model — real retrieval and
    # the real `timing.stage("rerank_load")`/`timing.stage("rerank_inference")` wraps in
    # `reranking.py` still run around them.
    monkeypatch.setattr(embeddings.OllamaEmbedder, "embed_one", lambda self, text: [0.1] * 1024)

    class _FakeCrossEncoder:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [1.0 for _ in pairs]

    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _FakeCrossEncoder())

    class _TwoCallChat:
        """Generate's answer first, groundedness's verdict second (same chat object, as a
        real request does: one generate call then one groundedness call)."""

        def __init__(self) -> None:
            self.calls = 0

        def chat_stream(self, *_a: object, usage: Usage | None = None, **_kw: object) -> Any:
            self.calls += 1
            if usage is not None:
                usage.add(Usage(prompt_tokens=10, completion_tokens=5))
            if self.calls == 1:
                yield "Use prepared statements "
                yield "to stop SQL injection [1]."
            else:
                yield "GROUNDED"

    monkeypatch.setattr(generation, "make_chat_client", lambda *_a, **_k: _TwoCallChat())

    caplog.set_level(logging.INFO, logger="rag_app.timing")
    user = _make_user(db_engine)
    events = _stream(client, user, "How do I prevent SQL injection?")
    assert events[-1]["type"] == "done"
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages == ["classifying", "retrieving", "reranking", "generating", "checking"]

    records = [r for r in caplog.records if r.name == "rag_app.timing"]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    expected_stage_keys = {
        "classify_ms",
        "embed_ms",
        "hybrid_search_ms",
        "rerank_load_ms",
        "rerank_inference_ms",
        "generate_ms",
        "groundedness_ms",
    }
    missing = expected_stage_keys - set(payload)
    assert not missing, f"missing stage keys: {missing}"
    assert payload["ttft_ms"] is not None
    assert payload["total_ms"] > 0
    _assert_no_orphans(db_engine, user)


# --- DA-11bC-2: concurrent /chat + /chat/stream on the shared reranker, real paths ----------


def test_concurrent_chat_and_chat_stream_requests_use_the_shared_reranker_correctly(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_shared_reranker()`` is ONE process-wide instance (T11.4.1); ``/chat`` (FastAPI's
    sync-route threadpool) and ``/chat/stream`` (its own worker thread, T11.2.16/DA-G2-2) both
    call its ``.rerank()``/``.predict()``. A real ``/chat`` request and a real
    ``/chat/stream`` request, about two different documents, fired at the same time (real
    threads, through the actual API — only the external Ollama embed/LLM calls and the heavy
    cross-encoder model are mocked): each must get back its own correct, uncorrupted answer —
    no cross-talk between the two concurrent requests sharing the one reranker instance."""
    import threading
    import time

    from rag_app import embeddings, generation, reranking
    from rag_app.db.models import Chunk, Document
    from rag_app.llm import Usage

    monkeypatch.setenv("RERANK_TOP_N", "1")  # isolates exactly the one matching chunk
    monkeypatch.setattr(reranking, "_shared_reranker", None)

    scenarios = [
        (
            "sql",
            "SQL Injection Prevention",
            "Use parameterized queries to stop SQL injection.",
            "How do I prevent SQL injection?",
            "parameterized queries",
        ),
        (
            "csrf",
            "CSRF Prevention",
            "Use a CSRF token on every state-changing request.",
            "How do I prevent CSRF?",
            "CSRF token",
        ),
    ]
    with Session(db_engine) as session:
        for slug, heading, text_, _question, _expect in scenarios:
            doc = Document(slug=f"da11bc2-{slug}", version="current")
            session.add(doc)
            session.flush()
            session.add(
                Chunk(
                    document_id=doc.id,
                    chunk_uid=f"da11bc2-{slug}::0",
                    heading=heading,
                    ordinal=0,
                    text=text_,
                    embedding=[0.1] * 1024,
                    version="current",
                )
            )
        session.commit()

    monkeypatch.setattr(embeddings.OllamaEmbedder, "embed_one", lambda self, text: [0.1] * 1024)

    active = 0
    max_active = 0
    lock = threading.Lock()

    class _SlowFakeCrossEncoder:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.1)  # releases the GIL, same technique as test_reranking.py
                scores = []
                for query_text, candidate_text in pairs:
                    matched = any(
                        key in query_text and key in candidate_text for key in ("SQL", "CSRF")
                    )
                    scores.append(1.0 if matched else 0.0)
                return scores
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _SlowFakeCrossEncoder())

    class _ContentAwareChat:
        """A fresh instance per pipeline run; answers from the ACTUAL retrieved content, not
        from which thread/question it happens to run in — a cross-contaminated reranker
        result would show up here as the wrong answer."""

        def __init__(self) -> None:
            self.calls = 0

        def _reply(self, messages: Any) -> str:
            self.calls += 1
            if self.calls > 1:
                return "GROUNDED"
            content = " ".join(str(m.get("content", "")) for m in messages)
            if "SQL" in content:
                return "Use parameterized queries [1]."
            if "CSRF" in content:
                return "Use a CSRF token [1]."
            return "I don't know [1]."

        def chat(self, messages: Any, **_kw: object) -> str:
            return self._reply(messages)

        def chat_stream(self, messages: Any, *, usage: Usage | None = None, **_kw: object) -> Any:
            if usage is not None:
                usage.add(Usage(prompt_tokens=10, completion_tokens=5))
            yield self._reply(messages)

    monkeypatch.setattr(generation, "make_chat_client", lambda *_a, **_k: _ContentAwareChat())

    answers: dict[str, str] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=10)

    def ask_stream(slug: str, question: str) -> None:
        try:
            barrier.wait()
            user = _make_user(db_engine)
            events = _stream(client, user, question)
            assert events[-1]["type"] == "done"
            answers[slug] = events[-1]["answer"]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def ask_sync(slug: str, question: str) -> None:
        try:
            barrier.wait()
            user = _make_user(db_engine)
            res = client.post("/chat", json={"question": question}, headers=_auth(user))
            assert res.status_code == 200, res.text
            answers[slug] = res.json()["answer"]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    (sql_slug, _h1, _t1, sql_q, sql_expect), (csrf_slug, _h2, _t2, csrf_q, csrf_expect) = scenarios
    t1 = threading.Thread(target=ask_stream, args=(sql_slug, sql_q))
    t2 = threading.Thread(target=ask_sync, args=(csrf_slug, csrf_q))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors, errors
    assert max_active >= 2, "the two concurrent requests never actually overlapped"
    assert sql_expect in answers[sql_slug] and csrf_expect not in answers[sql_slug]
    assert csrf_expect in answers[csrf_slug] and sql_expect not in answers[csrf_slug]
