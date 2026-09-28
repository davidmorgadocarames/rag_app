"""Unit tests for password hashing and JWT."""

from __future__ import annotations

import pytest

from rag_app.security import create_token, decode_token, hash_password, verify_password

# Throwaway HS256 keys: 32+ bytes (PyJWT warns on shorter HMAC keys) and deliberately
# low-entropy, so secret scanners do not mistake them for real credentials.
KEY_A = "a" * 40
KEY_B = "b" * 40


def test_password_roundtrip() -> None:
    digest = hash_password("correct horse battery staple")
    assert digest != "correct horse battery staple"  # not stored in plaintext
    assert verify_password("correct horse battery staple", digest) is True
    assert verify_password("wrong password", digest) is False


def test_jwt_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", KEY_A)
    token = create_token("user-1", expires_minutes=5)
    assert decode_token(token) == "user-1"


def test_jwt_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", KEY_A)
    assert decode_token("not-a-jwt") is None


def test_jwt_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", KEY_A)
    token = create_token("user-1")
    monkeypatch.setenv("JWT_SECRET", KEY_B)
    assert decode_token(token) is None


def test_jwt_rejects_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", KEY_A)
    token = create_token("user-1", expires_minutes=-1)
    assert decode_token(token) is None
