"""Server-side sessions (T12.1.2; P12-D3): a 256-bit random ID (``secrets.token_urlsafe(32)``)
carried by the ``__Host-secrag_session`` cookie — only its SHA-256 hash (``rag_app.security.
hash_token``, already used for e-mail verification tokens) is the primary key of the
``sessions`` table (0006), so a stolen row never yields a usable session on its own.

Idle timeout is refreshed on every throttled touch (``last_seen_at`` is written at most once
per ``LAST_SEEN_THROTTLE_SECONDS``); the absolute timeout is fixed at creation and never
extended, however active the session stays. Admin timeouts (shorter) are read from
``user.role`` — not available before 12b (T12.3.3), but the policy already applies to the
default ``'user'`` role today. These are code constants on purpose (B6): a session lifetime is
a security policy, not an Azure configuration value.

``authenticate`` is the single entry point ``get_current_user`` (``rag_app.api.auth``) uses
for the cookie path: an unknown, idle-expired, absolute-expired or already-revoked (row
missing) session all resolve to ``None`` — the caller cannot tell which, by design (DA-G2-1
style: no information disclosure through the rejection reason).
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rag_app.db.models import User, UserSession
from rag_app.security import hash_token

# Cookie name: the `__Host-` prefix (RFC 6265bis) requires `Secure`, `Path=/` and no `Domain`
# attribute — the browser then refuses to accept it from anything but this exact origin over
# HTTPS, which is why httpx/TestClient needs an `https://` base_url to keep it in tests (B7).
SESSION_COOKIE_NAME = "__Host-secrag_session"

# secrets.token_urlsafe(n) draws n random bytes: 32 = 256 bits (B6).
SESSION_ID_BYTES = 32

# P12-D3: ordinary users idle out after 30 min / 12 h absolute; admins (shorter-lived
# elevated sessions) 15 min / 8 h.
IDLE_TIMEOUT_MINUTES = 30
ABSOLUTE_TIMEOUT_HOURS = 12
ADMIN_IDLE_TIMEOUT_MINUTES = 15
ADMIN_ABSOLUTE_TIMEOUT_HOURS = 8
ADMIN_ROLE = "admin"

# last_seen_at (and the idle timeout it drives) is only written this often — never on every
# authenticated request — to keep the common path a read-only lookup.
LAST_SEEN_THROTTLE_SECONDS = 60

COOKIE_PATH = "/"


def _idle_timeout(role: str) -> dt.timedelta:
    minutes = ADMIN_IDLE_TIMEOUT_MINUTES if role == ADMIN_ROLE else IDLE_TIMEOUT_MINUTES
    return dt.timedelta(minutes=minutes)


def _absolute_timeout(role: str) -> dt.timedelta:
    hours = ADMIN_ABSOLUTE_TIMEOUT_HOURS if role == ADMIN_ROLE else ABSOLUTE_TIMEOUT_HOURS
    return dt.timedelta(hours=hours)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def create_session(session: Session, user: User, *, now: dt.datetime | None = None) -> str:
    """Insert a new session row for ``user``; return the RAW token (never stored — only its
    hash is). Caller commits."""
    moment = now if now is not None else _now()
    raw = secrets.token_urlsafe(SESSION_ID_BYTES)
    role = user.role or "user"
    session.add(
        UserSession(
            id=hash_token(raw),
            user_id=user.id,
            created_at=moment,
            last_seen_at=moment,
            idle_expires_at=moment + _idle_timeout(role),
            absolute_expires_at=moment + _absolute_timeout(role),
        )
    )
    return raw


def _row(session: Session, raw: str) -> UserSession | None:
    return session.scalar(select(UserSession).where(UserSession.id == hash_token(raw)))


def _delete_row(session: Session, session_id: str) -> None:
    session.execute(delete(UserSession).where(UserSession.id == session_id))


def authenticate(session: Session, raw: str, *, now: dt.datetime | None = None) -> User | None:
    """Unknown / idle-expired / absolute-expired / revoked (row missing) -> ``None``, and an
    expired row found along the way is deleted (lazy cleanup — the purger would get it later
    anyway). A still-valid session gets its ``last_seen_at``/``idle_expires_at`` touched,
    throttled to at most once per ``LAST_SEEN_THROTTLE_SECONDS``. Caller commits."""
    moment = now if now is not None else _now()
    row = _row(session, raw)
    if row is None:
        return None
    if moment >= row.idle_expires_at or moment >= row.absolute_expires_at:
        _delete_row(session, row.id)
        return None
    user = session.scalar(select(User).where(User.id == row.user_id, User.deleted_at.is_(None)))
    if user is None:  # erased since the session was created
        _delete_row(session, row.id)
        return None
    if (moment - row.last_seen_at) >= dt.timedelta(seconds=LAST_SEEN_THROTTLE_SECONDS):
        row.last_seen_at = moment
        row.idle_expires_at = moment + _idle_timeout(user.role or "user")
    return user


def revoke(session: Session, raw: str) -> None:
    """Delete the session named by ``raw``; a no-op if it does not exist. Caller commits."""
    session.execute(delete(UserSession).where(UserSession.id == hash_token(raw)))


def revoke_all(session: Session, user_id: uuid.UUID) -> None:
    """Delete every session of ``user_id`` (logout-everywhere, erasure). Caller commits."""
    session.execute(delete(UserSession).where(UserSession.user_id == user_id))


def rotate(session: Session, raw: str, *, now: dt.datetime | None = None) -> str | None:
    """Delete the session named by ``raw`` and create a fresh one (new ID) for the same
    user; ``None`` when ``raw`` does not name a live session (the old ID is still gone,
    whatever the reason — never silently kept). Caller commits."""
    moment = now if now is not None else _now()
    row = _row(session, raw)
    if row is None:
        return None
    user = session.scalar(select(User).where(User.id == row.user_id))
    _delete_row(session, row.id)
    if user is None:
        return None
    return create_session(session, user, now=moment)
