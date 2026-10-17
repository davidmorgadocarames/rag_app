"""rag_app.sessions (T12.1.2): unknown / idle-expired / absolute-expired / revoked sessions
are rejected, rotation invalidates the old ID, and last_seen writes are throttled."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from rag_app import sessions
from rag_app.db.models import User, UserSession
from rag_app.security import hash_password, hash_token

pytestmark = [pytest.mark.db, pytest.mark.security]


def _make_user(engine: Engine, *, role: str = "user") -> uuid.UUID:
    with Session(engine) as session:
        user = User(
            email=f"sess-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password("correct horse battery staple"),
            role=role,
        )
        session.add(user)
        session.commit()
        return user.id


def test_unknown_session_is_rejected(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        assert sessions.authenticate(session, "not-a-real-session-token") is None


def test_a_fresh_session_authenticates(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        raw = sessions.create_session(session, user)
        session.commit()
        authed = sessions.authenticate(session, raw)
        assert authed is not None and authed.id == user_id


def test_idle_expired_session_is_rejected(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        raw = sessions.create_session(session, user)
        session.commit()
        # Push the idle expiry into the past directly (absolute stays far in the future):
        # isolates the idle-timeout rejection from the absolute one.
        session.execute(
            update(UserSession)
            .where(UserSession.id == hash_token(raw))
            .values(idle_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
        session.commit()
        assert sessions.authenticate(session, raw) is None
        session.commit()
        # Lazily cleaned up: the row is gone, not just rejected.
        assert session.scalar(select(UserSession).where(UserSession.id == hash_token(raw))) is None


def test_absolute_expired_session_is_rejected(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        raw = sessions.create_session(session, user)
        session.commit()
        # Idle stays far in the future; only the absolute expiry is in the past.
        session.execute(
            update(UserSession)
            .where(UserSession.id == hash_token(raw))
            .values(absolute_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
        session.commit()
        assert sessions.authenticate(session, raw) is None
        session.commit()


def test_revoked_session_is_rejected(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        raw = sessions.create_session(session, user)
        session.commit()
        sessions.revoke(session, raw)
        session.commit()
        assert sessions.authenticate(session, raw) is None


def test_revoke_all_removes_every_session_of_the_user(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        raw_a = sessions.create_session(session, user)
        raw_b = sessions.create_session(session, user)
        session.commit()
        sessions.revoke_all(session, user_id)
        session.commit()
        assert sessions.authenticate(session, raw_a) is None
        assert sessions.authenticate(session, raw_b) is None


def test_rotate_invalidates_the_old_id(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        old_raw = sessions.create_session(session, user)
        session.commit()
        new_raw = sessions.rotate(session, old_raw)
        session.commit()
        assert new_raw is not None and new_raw != old_raw
        assert sessions.authenticate(session, old_raw) is None
        authed = sessions.authenticate(session, new_raw)
        assert authed is not None and authed.id == user_id


def test_rotate_of_an_unknown_session_returns_none(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        assert sessions.rotate(session, "not-a-real-session-token") is None


def test_last_seen_is_throttled_then_updated(db_engine: Engine) -> None:
    user_id = _make_user(db_engine)
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        t0 = dt.datetime.now(dt.UTC).replace(microsecond=0)
        raw = sessions.create_session(session, user, now=t0)
        session.commit()

        just_after = t0 + dt.timedelta(seconds=30)  # under the 60 s throttle
        assert sessions.authenticate(session, raw, now=just_after) is not None
        session.commit()
        row = session.scalar(select(UserSession).where(UserSession.id == hash_token(raw)))
        assert row is not None and row.last_seen_at == t0  # unchanged: still throttled

        later = t0 + dt.timedelta(seconds=90)  # over the throttle
        assert sessions.authenticate(session, raw, now=later) is not None
        session.commit()
        row = session.scalar(select(UserSession).where(UserSession.id == hash_token(raw)))
        assert row is not None and row.last_seen_at == later


def test_admin_role_gets_the_shorter_p12_d3_timeouts(db_engine: Engine) -> None:
    user_id = _make_user(db_engine, role="admin")
    with Session(db_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        t0 = dt.datetime.now(dt.UTC)
        raw = sessions.create_session(session, user, now=t0)
        session.commit()
        row = session.scalar(select(UserSession).where(UserSession.id == hash_token(raw)))
        assert row is not None
        assert row.idle_expires_at == t0 + dt.timedelta(minutes=sessions.ADMIN_IDLE_TIMEOUT_MINUTES)
        assert row.absolute_expires_at == t0 + dt.timedelta(
            hours=sessions.ADMIN_ABSOLUTE_TIMEOUT_HOURS
        )
        # The ordinary user's timeouts must actually be longer, or this test would pass for
        # the wrong reason.
        assert sessions.ADMIN_IDLE_TIMEOUT_MINUTES < sessions.IDLE_TIMEOUT_MINUTES
        assert sessions.ADMIN_ABSOLUTE_TIMEOUT_HOURS < sessions.ABSOLUTE_TIMEOUT_HOURS
