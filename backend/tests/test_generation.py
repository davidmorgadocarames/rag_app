"""Unit tests for the chit-chat classifier, token accounting, and stream shaping.

None of these require a live LLM or database — they exercise the pure logic that the
streaming pipeline is built on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from rag_app import generation, reranking
from rag_app.generation import (
    INSUFFICIENT,
    StreamResult,
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


# --- per-stage timing (T11.3.1): one JSON record per answer, no PII ----------------------


class _FakeChatSync:
    """Non-streaming ``chat()``: first call answers, second call is the groundedness verdict."""

    def __init__(self, answer_text: str, verdict: str = "GROUNDED") -> None:
        self._answer_text = answer_text
        self._verdict = verdict
        self.calls = 0

    def chat(self, *_a: object, **_kw: object) -> str:
        self.calls += 1
        return self._answer_text if self.calls == 1 else self._verdict


class _FakeChatStreamSeq:
    """Streaming ``chat_stream()``: one fixed delta sequence per call, in order."""

    def __init__(self, sequences: list[list[str]]) -> None:
        self._sequences = sequences
        self.calls = 0

    def chat_stream(self, *_a: object, usage: Usage | None = None, **_kw: object) -> Iterator[str]:
        seq = self._sequences[self.calls]
        self.calls += 1
        yield from seq
        if usage is not None:
            usage.add(Usage(prompt_tokens=10, completion_tokens=len(seq)))


def _timing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "rag_app.timing"]


def test_answer_question_emits_one_timing_record_with_no_pii(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    secret_question = "how do I fix this for my-email@example.com?"
    chat = _FakeChatSync("Use prepared statements [1].")

    answer = generation.answer_question(None, secret_question, chat=chat, use_rerank=False)  # type: ignore[arg-type]

    assert answer.grounded is True
    records = _timing_records(caplog)
    assert len(records) == 1
    line = records[0].getMessage()
    assert secret_question not in line
    assert "@" not in line
    payload = json.loads(line)
    assert payload["streaming"] is False
    assert payload["ttft_ms"] is not None
    # hybrid_search is stubbed above (no DB in this test), so only the stages measured by
    # answer_question/answer_from_chunks themselves are expected here.
    for key in ("generate_ms", "groundedness_ms", "total_ms"):
        assert key in payload


def test_answer_question_stream_emits_one_timing_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    monkeypatch.setattr(
        reranking.CrossEncoderReranker, "rerank", lambda _self, _q, chunks, _n: chunks
    )
    chat = _FakeChatStreamSeq([["Use prepared statements ", "[1]."], ["GROUNDED"]])

    events = list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
        )
    )

    results = [e for e in events if isinstance(e, StreamResult)]
    assert len(results) == 1
    assert results[0].answer.grounded is True
    records = _timing_records(caplog)
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["streaming"] is True
    assert payload["ttft_ms"] is not None
    for key in ("classify_ms", "generate_ms", "groundedness_ms", "total_ms"):
        assert key in payload


# --- T11.4.1: both generation paths default to the ONE shared reranker --------------------


def test_answer_question_uses_the_shared_reranker_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranking, "_shared_reranker", None)
    shared = reranking.get_shared_reranker()
    seen: list[object] = []

    def fake_retrieve(
        _session: object,
        _query: str,
        *,
        reranker: object,
        candidate_k: int | None = None,
        top_n: int | None = None,
        version: str | None = None,
    ) -> list[object]:
        seen.append(reranker)
        return [_chunk()]

    monkeypatch.setattr(reranking, "retrieve", fake_retrieve)
    chat = _FakeChatSync("Use prepared statements [1].")

    generation.answer_question(None, "q", chat=chat, use_rerank=True)  # type: ignore[arg-type]
    assert seen == [shared]


def test_answer_question_stream_uses_the_shared_reranker_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranking, "_shared_reranker", None)
    shared = reranking.get_shared_reranker()
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    seen: list[object] = []

    def fake_rerank(self: object, _query: str, chunks: list[object], _top_n: int) -> list[object]:
        seen.append(self)
        return chunks

    monkeypatch.setattr(reranking.CrossEncoderReranker, "rerank", fake_rerank)
    chat = _FakeChatStreamSeq([["Use ", "[1]."], ["GROUNDED"]])

    list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
        )
    )
    assert seen == [shared]


def test_answer_question_stream_chitchat_still_emits_one_timing_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The chit-chat fast path never calls the LLM: ttft stays None, only classify is timed."""
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    events = list(generation.answer_question_stream(None, "hi", chat=_FakeChatSync("")))  # type: ignore[arg-type]
    assert any(isinstance(e, StreamResult) for e in events)
    records = _timing_records(caplog)
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["streaming"] is True
    assert payload["ttft_ms"] is None
    assert "classify_ms" in payload
    assert "generate_ms" not in payload
