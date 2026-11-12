"""Password hashing (argon2) and JWT tokens.

``create_token``/``decode_token`` are no longer called anywhere in the API since T12.5.3
removed the legacy Bearer path (``rag_app.api.auth.get_current_user`` is cookie-only now,
and ``/auth/login`` no longer issues a token) -- kept here as plain, independently-tested
utility functions until T15.2.5's full JWT-infrastructure cleanup (``JWT_SECRET`` stays a
required setting until then too)."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from rag_app.config import get_settings

_hasher = PasswordHasher()
_ALGORITHM = "HS256"

_dummy_password_hash: str | None = None


def dummy_password_hash() -> str:
    """T12.4.1: a REAL argon2 hash of a fixed, never-issued password -- computed once per
    process (lazily, not at import time) and cached. ``login`` verifies against this hash
    when no account matches the submitted email, so the argon2 cost is paid on exactly the
    same code path whether the email exists or not: an attacker cannot time-distinguish
    "no such account" from "wrong password" (F3, user enumeration via timing, OWASP-A07).
    The random suffix only has to be stable within one process; it is never a real secret
    and never compared against a real password."""
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = _hasher.hash("not-a-real-account-" + secrets.token_hex(16))
    return _dummy_password_hash


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex of a token (store the hash, not the raw token)."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_token(subject: str, *, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    minutes = expires_minutes if expires_minutes is not None else settings.jwt_expires_minutes
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
    payload = {"sub": subject, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_token(token: str) -> str | None:
    """Return the subject if the token is valid and unexpired, else None."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None
