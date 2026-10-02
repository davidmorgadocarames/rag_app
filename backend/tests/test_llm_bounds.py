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
    """A ChatClient that records ``max_tokens``/``num_ctx``/``call_type`` and answers
    plausibly (T11.3.4 extends the original ``max_tokens``-only recorder)."""

    def __init__(self) -> None:
        self.bounds: list[tuple[str, int | None]] = []
        self.calls: list[tuple[str, int | None, int | None, str]] = []
        self._answered = False

    def _reply(self) -> str:
        if not self._answered:
            self._answered = True
            return "Use prepared statements [1]."
        return "GROUNDED"

    def chat(
        self,
        messages: Any,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> str:
        self.bounds.append(("chat", max_tokens))
        self.calls.append(("chat", max_tokens, num_ctx, call_type))
        return self._reply()

    def chat_stream(
        self,
        messages: Any,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        usage: Usage | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> Iterator[str]:
        self.bounds.append(("chat_stream", max_tokens))
        self.calls.append(("chat_stream", max_tokens, num_ctx, call_type))
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


# --- T11.3.4: explicit num_ctx per call type ------------------------------------------------


def test_chat_path_passes_num_ctx_per_call_type() -> None:
    chat = _RecordingChat()
    answer_from_chunks(chat, "How do I prevent SQL injection?", [_chunk()])  # type: ignore[arg-type]
    settings = get_settings()
    assert chat.calls == [
        ("chat", settings.max_tokens, settings.num_ctx_answer, "answer"),
        ("chat", GROUNDEDNESS_MAX_TOKENS, settings.num_ctx_groundedness, "groundedness"),
    ]


def test_stream_path_passes_num_ctx_per_call_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    chat = _RecordingChat()
    list(
        answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
            use_rerank=False,
        )
    )
    settings = get_settings()
    assert chat.calls == [
        ("chat_stream", settings.max_tokens, settings.num_ctx_answer, "answer"),
        ("chat_stream", GROUNDEDNESS_MAX_TOKENS, settings.num_ctx_groundedness, "groundedness"),
    ]


def test_num_ctx_answer_and_groundedness_must_match() -> None:
    """Measured on the development machine (ADR 11 decision 8): Ollama reloads the whole
    model (~6.3 s) whenever a request's ``num_ctx`` differs from the one it is currently
    loaded with — including a request that omits ``num_ctx`` (Ollama's own default, 2048,
    differs from either of these). One answer always calls generate then groundedness back
    to back, so a mismatch here would reload qwen twice per answer — a latency regression
    invisible to every quality/functional test. See config.py's ``num_ctx_answer`` comment."""
    settings = get_settings()
    assert settings.num_ctx_answer == settings.num_ctx_groundedness


def test_router_and_judge_calls_do_not_pin_a_num_ctx_yet() -> None:
    """T11.3.4 explicitly defers the agentic router (agentic.py) and the eval judge
    (eval/judge.py) to a later task ("router/summary... later") — neither is on the live API
    path today (generation.py docstring), so they still omit ``num_ctx`` (Ollama's default,
    2048) without the reload risk ``test_num_ctx_answer_and_groundedness_must_match`` guards."""
    from rag_app import agentic
    from rag_app.eval import judge

    for module in (agentic, judge):
        calls = _llm_calls(Path(module.__file__))
        assert calls, f"{module.__name__}: expected at least one LLM call"
        assert all(
            not any(kw.arg == "num_ctx" for kw in call.keywords) for call in calls
        ), f"{module.__name__}: a call now pins num_ctx — re-check the reload note above"
