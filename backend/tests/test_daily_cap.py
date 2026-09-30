"""Global daily answer cap (R6-1, T11.2.14) and the stream session factory lock (DA-G3-2).

Unit tests (no database): settings validation, ``Retry-After`` arithmetic, the factory lock.
``db`` tests on the harness database, through the real API:

- the answer is reserved in ``usage_daily`` at request START, in one statement: N concurrent
  requests at ``cap - 1`` → exactly one gets through (DA-G3-1);
- cap reached → no LLM call; ``/chat`` answers 429 + ``Retry-After`` until UTC midnight,
  ``/chat/stream`` ends with an ``error`` event carrying the conversation id;
- failed turns still count; tokens are added when known; the chit-chat canned reply (no LLM
  call) does not count; the counter starts again every UTC day.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from rag_app.config import Settings, SettingsValidationError, validate_api_settings
from rag_app.usage_cap import DAILY_CAP_CODE, DAILY_CAP_MESSAGE, seconds_until_utc_midnight

QUESTION = "How do I prevent SQL injection?"  # a security question: never the canned path


# --- unit: settings, Retry-After, factory lock ---------------------------------------------


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://u:p@127.0.0.1:15432/cap_unit",
        "jwt_secret": "j" * 32,
        "data_master_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_the_default_cap_is_on_and_valid_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAILY_ANSWER_CAP", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    settings = _settings()
    assert settings.env == "prod" and settings.daily_answer_cap == 300
    validate_api_settings(settings)


def test_prod_requires_a_positive_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV", raising=False)
    with pytest.raises(SettingsValidationError, match="DAILY_ANSWER_CAP must be positive"):
        validate_api_settings(_settings(daily_answer_cap=0))


def test_dev_may_switch_the_cap_off_but_never_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    validate_api_settings(_settings(daily_answer_cap=0))
    with pytest.raises(SettingsValidationError, match="DAILY_ANSWER_CAP must be 0"):
        validate_api_settings(_settings(daily_answer_cap=-1))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (dt.datetime(2026, 9, 30, 23, 59, 30, tzinfo=dt.UTC), 30),
        (dt.datetime(2026, 9, 30, 0, 0, 0, tzinfo=dt.UTC), 86_400),
        (dt.datetime(2026, 9, 30, 23, 59, 59, 900_000, tzinfo=dt.UTC), 1),
        # 01:00 in UTC+2 is 23:00 UTC the day before: one hour left.
        (dt.datetime(2026, 10, 1, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))), 3_600),
    ],
)
def test_retry_after_counts_to_the_next_utc_midnight(now: dt.datetime, expected: int) -> None:
    assert seconds_until_utc_midnight(now) == expected


def test_the_stream_session_factory_is_built_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-G3-2: two (here eight) first streams at once build ONE engine, not one each."""
    from rag_app.api import conversations

    built: list[object] = []

    def slow_factory() -> object:
        time.sleep(0.05)  # widen the window between the None check and the assignment
        factory = object()
        built.append(factory)
        return factory

    monkeypatch.setattr(conversations, "make_session_factory", slow_factory)
    monkeypatch.setattr(conversations, "_stream_sessions", None)
    barrier = threading.Barrier(8)
    got: list[object] = []

    def first_use() -> None:
        barrier.wait()
        got.append(conversations._stream_session_factory())

    threads = [threading.Thread(target=first_use) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(built) == 1, f"{len(built)} engines built for one process"
    assert len(got) == 8 and all(f is built[0] for f in got)


# --- db: the counter through the real API ----------------------------------------------------


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")  # any real LLM call would fail
    monkeypatch.setenv("DAILY_ANSWER_CAP", "3")


@pytest.fixture()
def client(env: None, db_engine: Engine) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.api.deps import get_rate_limiter
    from rag_app.ratelimit import RateLimiter

    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM usage_daily"))
    app = create_app()
    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(100_000, 1_000.0)
    yield TestClient(app)


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _set_answers(engine: Engine, answers: int, tokens: int = 0) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO usage_daily (day, answers, tokens) VALUES (:d, :a, :t)"
                " ON CONFLICT (day) DO UPDATE SET answers = :a, tokens = :t"
            ),
            {"d": _today(), "a": answers, "t": tokens},
        )


def _usage(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT answers, tokens FROM usage_daily WHERE day = :d"), {"d": _today()}
        ).first()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _make_user(engine: Engine) -> uuid.UUID:
    from rag_app.crypto import generate_user_key, wrap_key
    from rag_app.db.models import User, UserKey
    from rag_app.security import hash_password

    with Session(engine) as session:
        user = User(
            email=f"cap-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password("correct horse battery"),
        )
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
        session.commit()
        return user.id


def _make_conversation(engine: Engine, user_id: uuid.UUID) -> str:
    from rag_app.db.models import Conversation

    with Session(engine) as session:
        conv = Conversation(user_id=user_id)
        session.add(conv)
        session.commit()
        return str(conv.id)


def _auth(user_id: uuid.UUID) -> dict[str, str]:
    from rag_app.security import create_token

    return {"Authorization": f"Bearer {create_token(str(user_id))}"}


def _events(res: Any) -> list[dict]:
    assert res.status_code == 200
    return [
        json.loads(frame[len("data: ") :])
        for frame in res.text.split("\n\n")
        if frame.startswith("data: ")
    ]


class _FakePipeline:
    """Stands in for ``answer_question_stream``: counts calls (= LLM calls it would make)."""

    def __init__(self, *, fail: bool = False, tokens: tuple[int, int] = (10, 5)) -> None:
        self.calls = 0
        self.fail = fail
        self.tokens = tokens
        self._lock = threading.Lock()

    def __call__(self, _session: Session, _question: str, **_kw: Any) -> Iterator[Any]:
        from rag_app.generation import Answer, StreamResult, StreamStage, StreamToken
        from rag_app.llm import Usage

        with self._lock:
            self.calls += 1
        yield StreamStage("generating")
        if self.fail:
            raise RuntimeError("the model went away mid-answer")
        yield StreamToken("Use prepared statements.")
        usage = Usage(*self.tokens)
        yield StreamResult(Answer(text="Use prepared statements."), usage=usage)


@pytest.mark.db
def test_concurrent_streams_at_cap_minus_one_let_exactly_one_through(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-G3-1: the check and the increment are one statement — 8 requests racing for the
    last answer of the day get exactly one answer and 7 refusals (no LLM call for them)."""
    from rag_app.api import conversations

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    user = _make_user(db_engine)
    conv = _make_conversation(db_engine, user)
    _set_answers(db_engine, 2)  # cap 3 → one answer left
    barrier = threading.Barrier(8)
    results: list[list[dict]] = []
    lock = threading.Lock()

    def ask() -> None:
        barrier.wait()
        res = client.post(
            "/chat/stream",
            json={"question": QUESTION, "conversation_id": conv},
            headers=_auth(user),
        )
        with lock:
            results.append(_events(res))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 8
    done = [r for r in results if r[-1]["type"] == "done"]
    refused = [r for r in results if r[-1].get("code") == DAILY_CAP_CODE]
    assert len(done) == 1 and len(refused) == 7
    for events in refused:
        assert [e["type"] for e in events] == ["conversation", "error"]
        assert events[-1]["conversation_id"] == conv
        assert events[-1]["detail"] == DAILY_CAP_MESSAGE
    assert pipeline.calls == 1, "a refused request reached the pipeline (LLM)"
    assert _usage(db_engine) == (3, 15)


@pytest.mark.db
def test_chat_at_the_cap_is_a_429_with_retry_after_and_no_llm_call(
    client: Any, db_engine: Engine
) -> None:
    from rag_app.api.auth import get_current_user
    from rag_app.api.deps import get_answerer
    from rag_app.generation import Answer

    calls: list[str] = []

    def answerer() -> Any:
        def _answer(_session: Any, question: str, _version: str | None) -> Answer:
            calls.append(question)
            return Answer(text="Use prepared statements.")

        return _answer

    app = client.app
    app.dependency_overrides[get_answerer] = answerer
    app.dependency_overrides[get_current_user] = lambda: object()

    _set_answers(db_engine, 2)
    invalid = client.post("/chat", json={"question": ""})
    assert invalid.status_code == 422 and _usage(db_engine)[0] == 2  # never counted
    ok = client.post("/chat", json={"question": QUESTION})
    assert ok.status_code == 200 and len(calls) == 1
    assert _usage(db_engine)[0] == 3

    refused = client.post("/chat", json={"question": QUESTION})
    assert refused.status_code == 429
    assert refused.json()["detail"] == DAILY_CAP_MESSAGE
    retry_after = int(refused.headers["Retry-After"])
    assert 1 <= retry_after <= 86_400
    assert len(calls) == 1, "the LLM was called after the cap was reached"
    assert _usage(db_engine)[0] == 3


@pytest.mark.db
def test_a_failed_turn_still_counts(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-G3-1: counted at request start — a turn that fails (or is interrupted) after the
    LLM started has spent tokens and uses up one answer."""
    from rag_app.api import conversations

    monkeypatch.setattr(conversations, "answer_question_stream", _FakePipeline(fail=True))
    user = _make_user(db_engine)
    events = _events(client.post("/chat/stream", json={"question": QUESTION}, headers=_auth(user)))
    assert events[-1]["code"] == "generation_failed"
    assert _usage(db_engine)[0] == 1


@pytest.mark.db
def test_an_interrupted_turn_still_counts(
    env: None, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reservation happens in the endpoint, before the body is streamed: a client that
    goes away mid-answer has already been counted."""
    from fastapi.testclient import TestClient

    from rag_app.api import conversations
    from rag_app.api.app import create_app

    closed: list[bool] = []
    real_chat_events = conversations._chat_events

    def chat_events_closed_early(*args: Any, **kwargs: Any) -> Iterator[str]:
        stream = real_chat_events(*args, **kwargs)
        yield next(stream)  # the conversation event, then the client goes away
        stream.close()
        closed.append(True)

    monkeypatch.setattr(conversations, "answer_question_stream", _FakePipeline())
    monkeypatch.setattr(conversations, "_chat_events", chat_events_closed_early)
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM usage_daily"))
    user = _make_user(db_engine)
    res = TestClient(create_app()).post(
        "/chat/stream", json={"question": QUESTION}, headers=_auth(user)
    )
    assert res.status_code == 200 and closed
    assert _usage(db_engine)[0] == 1


@pytest.mark.db
def test_a_new_chat_at_the_cap_stores_nothing(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_app.api import conversations

    pipeline = _FakePipeline()
    monkeypatch.setattr(conversations, "answer_question_stream", pipeline)
    user = _make_user(db_engine)
    _set_answers(db_engine, 3)
    events = _events(client.post("/chat/stream", json={"question": QUESTION}, headers=_auth(user)))
    assert [e["type"] for e in events] == ["conversation", "error"]
    assert events[-1]["code"] == DAILY_CAP_CODE and events[-1]["conversation_id"] is None
    assert pipeline.calls == 0
    with db_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM conversations WHERE user_id = :u"), {"u": user}
        ).scalar_one()
    assert n == 0


@pytest.mark.db
def test_the_canned_greeting_is_not_counted_and_works_at_the_cap(
    client: Any, db_engine: Engine
) -> None:
    """The chit-chat fast path makes no LLM call: it neither counts nor is refused."""
    user = _make_user(db_engine)
    _set_answers(db_engine, 3)
    events = _events(client.post("/chat/stream", json={"question": "hello"}, headers=_auth(user)))
    assert events[-1]["type"] == "done"
    assert _usage(db_engine) == (3, 0)


@pytest.mark.db
def test_the_counter_starts_again_every_utc_day(db_engine: Engine) -> None:
    from rag_app.usage_cap import add_tokens, reserve_answer

    day1 = dt.datetime(2031, 1, 1, 23, 59, tzinfo=dt.UTC)
    day2 = dt.datetime(2031, 1, 2, 0, 1, tzinfo=dt.UTC)
    with Session(db_engine) as session:
        assert [reserve_answer(session, 2, now=day1) for _ in range(3)] == [True, True, False]
        add_tokens(session, 40, now=day1)
        assert reserve_answer(session, 2, now=day2) is True
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT day, answers, tokens FROM usage_daily WHERE day >= '2031-01-01'"
                " ORDER BY day"
            )
        ).all()
    assert [(r[0].isoformat(), r[1], r[2]) for r in rows] == [
        ("2031-01-01", 2, 40),
        ("2031-01-02", 1, 0),
    ]


@pytest.mark.db
def test_the_cap_reached_log_line_has_no_personal_data(
    client: Any, db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """The metric/log line for the Phase 13 banner: day and cap only."""
    from rag_app.api import conversations

    monkeypatch.setattr(conversations, "answer_question_stream", _FakePipeline())
    user = _make_user(db_engine)
    _set_answers(db_engine, 3)
    with caplog.at_level("WARNING", logger="rag_app.usage_cap"):
        client.post("/chat/stream", json={"question": QUESTION}, headers=_auth(user))
    lines = [r.getMessage() for r in caplog.records if r.name == "rag_app.usage_cap"]
    assert lines and all("daily answer cap reached" in line for line in lines)
    assert all(str(user) not in line and "@" not in line and QUESTION not in line for line in lines)
