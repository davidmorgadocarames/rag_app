"""Unit tests for the chit-chat classifier, token accounting, and stream shaping.

None of these require a live LLM or database — they exercise the pure logic that the
streaming pipeline is built on.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rag_app.generation import (
    INSUFFICIENT,
    StreamToken,
    _stream_answer_tokens,
    classify_intent,
)
from rag_app.llm import Usage
from rag_app.retrieval import RetrievedChunk


@pytest.mark.parametrize(
    "query",
    [
        "hi",
        "Hello!",
        "hey",
        "good morning",
        "thanks",
        "thank you",
        "who are you?",
        "what can you do",
        "bye",
    ],
)
def test_classify_intent_chitchat(query: str) -> None:
    assert classify_intent(query) == "chitchat"


@pytest.mark.parametrize(
    "query",
    [
        "How do I prevent SQL injection?",
        "What is broken access control?",
        "Explain OWASP A03",
        "how did injection change between 2021 and 2025",
        "sanitize user input for command execution",
        "thanks, now how do I stop XSS?",  # thanks present but security term wins
    ],
)
def test_classify_intent_security(query: str) -> None:
    assert classify_intent(query) == "security"


def test_usage_accumulates() -> None:
    total = Usage()
    total.add(Usage(prompt_tokens=10, completion_tokens=5))
    total.add(Usage(prompt_tokens=3, completion_tokens=7))
    assert total.prompt_tokens == 13
    assert total.completion_tokens == 12
    assert total.total_tokens == 25


class _FakeChat:
    """Minimal stand-in exposing chat_stream over a fixed list of deltas."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

    def chat_stream(
        self, *_args: object, usage: Usage | None = None, **_kw: object
    ) -> Iterator[str]:
        yield from self._deltas
        if usage is not None:
            usage.add(Usage(prompt_tokens=100, completion_tokens=len(self._deltas)))


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid="c1", heading="h", text="t", version="2021", effective_date=None, score=1.0
    )


def test_stream_hides_abstention_sentinel() -> None:
    chat = _FakeChat(list(INSUFFICIENT))  # stream the sentinel char-by-char
    usage = Usage()
    items = list(_stream_answer_tokens(chat, "q", [_chunk()], usage))  # type: ignore[arg-type]
    tokens = [i for i in items if isinstance(i, StreamToken)]
    assert tokens == []  # nothing streamed to the user
    assert items[-1] == INSUFFICIENT  # full raw text yielded last
    assert usage.prompt_tokens == 100


def test_stream_emits_real_answer_tokens() -> None:
    chat = _FakeChat(["Use ", "parameterized ", "queries ", "[1]"])
    usage = Usage()
    items = list(_stream_answer_tokens(chat, "q", [_chunk()], usage))  # type: ignore[arg-type]
    tokens = "".join(i.text for i in items if isinstance(i, StreamToken))
    assert tokens == "Use parameterized queries [1]"
    assert items[-1] == "Use parameterized queries [1]"
