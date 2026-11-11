"""Unit tests for password hashing and JWT."""

from __future__ import annotations

import secrets
import time

import pytest

from rag_app.security import (
    create_token,
    decode_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)

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


# --- T12.4.1: constant-time login (F3, user enumeration via timing) ------------------------


def test_dummy_password_hash_is_a_real_verifiable_argon2_hash() -> None:
    """``dummy_password_hash()`` must be a genuine argon2 hash (so ``verify_password`` pays
    the real argon2 cost against it), stable within one process, and never a password any
    real account could plausibly have."""
    digest = dummy_password_hash()
    assert digest == dummy_password_hash()  # cached, stable within the process
    assert digest.startswith("$argon2")
    assert verify_password("anything", digest) is False


def test_login_timing_does_not_distinguish_a_real_account_from_a_missing_one() -> None:
    """T12.4.1 (F3, OWASP-A07): the login route calls ``verify_password`` against the REAL
    hash when the account exists and against ``dummy_password_hash()`` when it does not --
    both are genuine argon2 hashes, so the verify cost is paid identically either way. This
    measures the hash-verify step directly (no HTTP/DB round trip): argon2's own cost
    (tens of milliseconds per call) dominates any OS-scheduling jitter at this repetition
    count, which is what keeps the tolerance below both robust on a loaded machine AND tight
    enough to actually catch a regression to the old "skip verify_password entirely when no
    user matches" shape (that regression would make the "missing account" path close to
    instant -- a ratio near 0, nothing like the ~1x this test allows)."""
    real_hash = hash_password("a-real-password-" + secrets.token_hex(8))
    dummy_hash = dummy_password_hash()
    repeats = 15

    def _measure(digest: str) -> float:
        start = time.perf_counter()
        for _ in range(repeats):
            verify_password("a-wrong-guess", digest)
        return (time.perf_counter() - start) / repeats

    # Warm up (first argon2 call sometimes pays a one-off cost, e.g. CPU feature detection)
    # so the measured loops below are not skewed by whichever branch happens to run first.
    verify_password("warmup", real_hash)
    verify_password("warmup", dummy_hash)

    real_avg = _measure(real_hash)
    dummy_avg = _measure(dummy_hash)
    ratio = max(real_avg, dummy_avg) / min(real_avg, dummy_avg)
    assert ratio < 2.0, (
        f"timing differs too much between a real account ({real_avg * 1000:.1f} ms/call) and"
        f" a missing one ({dummy_avg * 1000:.1f} ms/call) -- an attacker could time-distinguish"
        " them (tolerance documented above: both pay the same real argon2 cost)"
    )
