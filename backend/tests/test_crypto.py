"""Unit tests for per-user encryption (crypto-shred building blocks)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from rag_app.crypto import decrypt, encrypt, generate_user_key, unwrap_key, wrap_key


def _set_master(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())


def test_key_wrap_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_master(monkeypatch)
    key = generate_user_key()
    assert unwrap_key(wrap_key(key)) == key


def test_content_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_master(monkeypatch)
    key = generate_user_key()
    assert decrypt(key, encrypt(key, "secret message")) == "secret message"


def test_wrong_key_cannot_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_master(monkeypatch)
    key_a = generate_user_key()
    key_b = generate_user_key()
    token = encrypt(key_a, "secret")
    with pytest.raises(InvalidToken):
        decrypt(key_b, token)
