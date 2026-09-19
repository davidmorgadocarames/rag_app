"""Password hashing (argon2) and JWT tokens."""

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
