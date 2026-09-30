"""Every LLM call is bounded (DA-31b-3): the worst-case cost of one answer is fixed.

- A static check: every ``.chat(...)`` / ``.chat_stream(...)`` call in ``rag_app`` passes
  ``max_tokens`` (a new call without it fails here).
- Behaviour: both answer paths (``answer_from_chunks`` for ``/chat``, ``answer_question_stream``
  for ``/chat/stream``) call the client with a bound: generation ``Settings.max_tokens``
  (1024), the groundedness check ``GROUNDEDNESS_MAX_TOKENS`` (16).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import rag_app
from rag_app import generation
from rag_app.config import Settings, get_settings
from rag_app.generation import (
    GROUNDEDNESS_MAX_TOKENS,
    StreamResult,
    answer_from_chunks,
    answer_question_stream,
)
from rag_app.llm import Usage
from rag_app.retrieval import RetrievedChunk

_SRC = Path(rag_app.__file__).parent
_LLM_METHODS = {"chat", "chat_stream"}


def _llm_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LLM_METHODS
    ]


def test_every_llm_call_in_the_code_passes_max_tokens() -> None:
    missing = [
        f"{path.relative_to(_SRC)}:{call.lineno}"
        for path in sorted(_SRC.rglob("*.py"))
        for call in _llm_calls(path)
        if not any(kw.arg == "max_tokens" for kw in call.keywords)
    ]
    assert missing == [], f"LLM calls without max_tokens: {missing}"


def test_the_static_check_sees_the_known_call_sites() -> None:
    """Guard against a vacuous pass: generation (2 + 2), query rewrite, eval judge."""
    assert sum(len(_llm_calls(p)) for p in _SRC.rglob("*.py")) >= 6


class _RecordingChat:
    """A ChatClient that records ``max_tokens`` of every call and answers plausibly."""

    def __init__(self) -> None:
        self.bounds: list[tuple[str, int | None]] = []
        self._answered = False

    def _reply(self) -> str:
        if not self._answered:
            self._answered = True
            return "Use prepared statements [1]."
        return "GROUNDED"

    def chat(
        self, messages: Any, *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> str:
        self.bounds.append(("chat", max_tokens))
        return self._reply()

    def chat_stream(
        self,
        messages: Any,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        usage: Usage | None = None,
    ) -> Iterator[str]:
        self.bounds.append(("chat_stream", max_tokens))
        yield self._reply()


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid="owasp::1",
        heading="SQL Injection Prevention",
        text="Use parameterized queries (prepared statements) to prevent SQL injection.",
        version="cheatsheets",
        effective_date=None,
        score=0.9,
    )


def test_chat_path_bounds_generation_and_groundedness() -> None:
    chat = _RecordingChat()
    answer = answer_from_chunks(chat, "How do I prevent SQL injection?", [_chunk()])  # type: ignore[arg-type]
    assert not answer.abstained
    assert chat.bounds == [
        ("chat", get_settings().max_tokens),
        ("chat", GROUNDEDNESS_MAX_TOKENS),
    ]


def test_stream_path_bounds_generation_and_groundedness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    chat = _RecordingChat()
    events = list(
        answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
            use_rerank=False,
        )
    )
    result = events[-1]
    assert isinstance(result, StreamResult) and not result.answer.abstained
    assert chat.bounds == [
        ("chat_stream", get_settings().max_tokens),
        ("chat_stream", GROUNDEDNESS_MAX_TOKENS),
    ]


def test_the_bounds_are_the_documented_values() -> None:
    """ADR 11 "Costs" computes the worst case per answer from these two numbers."""
    assert Settings.model_fields["max_tokens"].default == 1024
    assert GROUNDEDNESS_MAX_TOKENS == 16
