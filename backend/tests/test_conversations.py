"""Unit tests for conversation encryption helpers (no database required)."""

from __future__ import annotations

from rag_app.api.conversations import (
    _decrypt_json,
    _encrypt_json,
    _title_for,
    _total_tokens,
)
from rag_app.crypto import encrypt, generate_user_key
from rag_app.db.models import Conversation, Message


def test_message_json_round_trip() -> None:
    key = generate_user_key()
    payload = {"text": "How do I prevent SQL injection?", "abstained": False}
    blob = _encrypt_json(key, payload)
    assert blob != payload["text"].encode()  # actually encrypted
    assert _decrypt_json(key, blob) == payload


def test_title_uses_encrypted_override() -> None:
    key = generate_user_key()
    conv = Conversation(title_encrypted=None)
    conv.title_encrypted = encrypt(key, "My renamed chat")
    assert _title_for(key, conv, []) == "My renamed chat"


def test_title_derives_from_first_user_message() -> None:
    key = generate_user_key()
    conv = Conversation(title_encrypted=None)
    messages = [
        Message(role="user", content_encrypted=_encrypt_json(key, {"text": "What is XSS?"})),
        Message(role="assistant", content_encrypted=_encrypt_json(key, {"text": "..."})),
    ]
    assert _title_for(key, conv, messages) == "What is XSS?"


def test_title_defaults_when_empty() -> None:
    key = generate_user_key()
    assert _title_for(key, Conversation(title_encrypted=None), []) == "New chat"


def test_malicious_title_round_trips_verbatim() -> None:
    """A SQL-injection-looking title is stored encrypted and returned literally.

    The title is Fernet-encrypted and written via the ORM as a bound parameter, so it
    is treated as opaque data — never executed. This documents the rename defense.
    """
    key = generate_user_key()
    evil = "'; DROP TABLE conversations;-- \" OR 1=1"
    conv = Conversation(title_encrypted=encrypt(key, evil))
    assert _title_for(key, conv, []) == evil


def test_total_tokens_treats_null_as_zero() -> None:
    messages = [
        Message(role="user", content_encrypted=b"", prompt_tokens=None, completion_tokens=None),
        Message(role="assistant", content_encrypted=b"", prompt_tokens=100, completion_tokens=40),
    ]
    assert _total_tokens(messages) == 140
