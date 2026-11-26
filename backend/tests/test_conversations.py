"""Unit tests for conversation encryption helpers (no database required)."""

from __future__ import annotations

from rag_app.api.conversations import (
    _decrypt_json,
    _decrypted_turns,
    _encrypt_json,
    _load_memory,
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


# --- T14.2.5: memory read helpers (pure, no database) ---------------------------------------


def test_decrypted_turns_skips_a_message_that_fails_to_decrypt() -> None:
    key = generate_user_key()
    other_key = generate_user_key()
    messages = [
        Message(role="user", content_encrypted=_encrypt_json(key, {"text": "q1"})),
        # Encrypted with a DIFFERENT key -- simulates a damaged/foreign row.
        Message(role="assistant", content_encrypted=_encrypt_json(other_key, {"text": "boom"})),
        Message(role="assistant", content_encrypted=_encrypt_json(key, {"text": "a1"})),
    ]
    assert _decrypted_turns(key, messages) == [("user", "q1", False), ("assistant", "a1", False)]


def test_decrypted_turns_carries_the_error_flag() -> None:
    key = generate_user_key()
    messages = [
        Message(role="user", content_encrypted=_encrypt_json(key, {"text": "q1"})),
        Message(
            role="assistant",
            content_encrypted=_encrypt_json(key, {"text": "oops", "error": True}),
        ),
    ]
    assert _decrypted_turns(key, messages) == [
        ("user", "q1", False),
        ("assistant", "oops", True),
    ]


def test_load_memory_is_none_none_for_a_brand_new_conversation() -> None:
    key = generate_user_key()
    conv = Conversation(summary_encrypted=None)
    assert _load_memory(key, conv, []) == (None, None, None)


def test_load_memory_reads_the_summary_and_the_last_clean_turn() -> None:
    key = generate_user_key()
    conv = Conversation(summary_encrypted=encrypt(key, '{"topics": ["sqli"]}'))
    messages = [
        Message(role="user", content_encrypted=_encrypt_json(key, {"text": "q1"})),
        Message(role="assistant", content_encrypted=_encrypt_json(key, {"text": "a1"})),
    ]
    summary, last_turn, previous_intent = _load_memory(key, conv, messages)
    assert summary == '{"topics": ["sqli"]}'
    assert last_turn == "User: q1\nAssistant: a1"
    assert previous_intent is None  # DA-router-reliability-1: no last_intent in this summary


def test_load_memory_reads_the_previous_intent_from_the_summary() -> None:
    """DA-router-reliability-1 (F24-into-14): ``last_intent``, piggybacked onto the stored
    ``ConversationSummary``, round-trips through ``_load_memory`` as ``previous_intent``."""
    key = generate_user_key()
    conv = Conversation(
        summary_encrypted=encrypt(key, '{"topics": ["sqli"], "last_intent": "ambiguous"}')
    )
    _summary, _last_turn, previous_intent = _load_memory(key, conv, [])
    assert previous_intent == "ambiguous"


def test_load_memory_never_crosses_conversations() -> None:
    """Authorization (T14.2.5): memory is scoped to the ONE conversation passed in --
    another conversation's messages/summary, even for the SAME user, never leak in because
    the caller never passes them. This is enforced by construction (there is no lookup by
    user_id here at all), demonstrated with two independent conversations."""
    key = generate_user_key()
    conv_a = Conversation(summary_encrypted=encrypt(key, '{"topics": ["a-only"]}'))
    conv_b = Conversation(summary_encrypted=encrypt(key, '{"topics": ["b-only"]}'))
    messages_a = [
        Message(role="user", content_encrypted=_encrypt_json(key, {"text": "about A"})),
        Message(role="assistant", content_encrypted=_encrypt_json(key, {"text": "reply A"})),
    ]
    messages_b = [
        Message(role="user", content_encrypted=_encrypt_json(key, {"text": "about B"})),
        Message(role="assistant", content_encrypted=_encrypt_json(key, {"text": "reply B"})),
    ]
    summary_a, last_turn_a, _pi_a = _load_memory(key, conv_a, messages_a)
    summary_b, last_turn_b, _pi_b = _load_memory(key, conv_b, messages_b)
    assert summary_a == '{"topics": ["a-only"]}' and "b-only" not in summary_a
    assert summary_b == '{"topics": ["b-only"]}' and "a-only" not in summary_b
    assert last_turn_a == "User: about A\nAssistant: reply A"
    assert last_turn_b == "User: about B\nAssistant: reply B"


def test_load_memory_treats_a_summary_from_another_key_as_unreadable_not_a_crash() -> None:
    key = generate_user_key()
    wrong_key = generate_user_key()
    conv = Conversation(summary_encrypted=encrypt(wrong_key, '{"topics": ["x"]}'))
    summary, _last_turn, previous_intent = _load_memory(key, conv, [])
    assert summary is None
    assert previous_intent is None
