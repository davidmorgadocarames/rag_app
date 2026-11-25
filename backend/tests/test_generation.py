"""Unit tests for the chit-chat classifier, token accounting, and stream shaping.

None of these require a live LLM or database — they exercise the pure logic that the
streaming pipeline is built on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from rag_app import generation, reranking, timing
from rag_app.assistant.router import RouterOutput
from rag_app.generation import (
    INSUFFICIENT,
    StreamResult,
    StreamStage,
    StreamStep,
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


@pytest.mark.parametrize(
    "query",
    [
        "hol",  # missing one letter from "hola"
        "holaa",  # one repeated letter
        "helo",  # missing one letter from "hello"
        "graicas",  # transposition of "gracias"
        "heeey",  # repeated letters collapse to "hey"
        "thnks",  # missing one letter from "thanks"
    ],
)
def test_classify_intent_typo_tolerant_chitchat(query: str) -> None:
    """T12.3.0c (DEBT-040): a typo'd short greeting/farewell/thanks is still chitchat."""
    assert classify_intent(query) == "chitchat"


@pytest.mark.parametrize(
    "query",
    [
        "how",  # too far from any lexicon word to be a believable typo
        "sql",  # security term, forces "security" before the typo check ever runs
        "hold on",  # not a greeting typo, even though it starts like one
        "xss",  # security term
    ],
)
def test_classify_intent_typo_tolerant_chitchat_does_not_misfire(query: str) -> None:
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


def test_token_stream_step_is_recorded_even_on_a_mid_stream_disconnect() -> None:
    """DA-13-G1-3: closing the generator mid-stream (a client disconnect throws
    ``GeneratorExit`` at whatever ``yield`` is currently suspended) must still record a
    PARTIAL ``token_stream`` duration -- the metric stays honest instead of silently
    dropping it, a third case beyond the two already-documented "nothing streamed" ones."""
    # First delta alone is >= len(INSUFFICIENT): it yields immediately and sets
    # ``emitting``, so every later delta also yields immediately (one `next()` per delta) --
    # needed so we can close the generator after the first delta but before the stream (and
    # the normal end-of-loop recording) is exhausted.
    chat = _FakeChat(["X" * 25, "second chunk", "third chunk"])
    usage = Usage()
    recorded: list[tuple[str, str, float]] = []

    def on_step(stage: str, step: str, elapsed_s: float) -> None:
        recorded.append((stage, step, elapsed_s))

    with timing.recorder(streaming=True, on_step=on_step):
        gen = _stream_answer_tokens(chat, "q", [_chunk()], usage)  # type: ignore[arg-type]
        next(gen)  # consumes "X"*25 -> llm_first_token recorded, first StreamToken yielded
        next(gen)  # consumes "second chunk" -> yields again (emitting == True)
        gen.close()  # simulate a client disconnect before "third chunk" is ever consumed

    token_stream = [r for r in recorded if r[0] == "generating" and r[1] == "token_stream"]
    assert len(token_stream) == 1  # recorded exactly once (never dropped, never double-counted)
    assert token_stream[0][2] >= 0.0


# --- per-stage timing (T11.3.1): one JSON record per answer, no PII ----------------------


class _FakeChatSync:
    """Non-streaming ``chat()``: first call answers, second call is the groundedness verdict.

    ``chat_json`` (T14.2.3's router call, block 14-B2): always returns the caller's
    ``fallback`` -- same minimal fake as ``test_llm_bounds.py::_RecordingChat``. Every
    security-path query in this file is plain English security text, so the router's
    conservative fallback (intent "security", the raw query) IS the behaviour under test;
    it never touches ``self.calls`` (a separate counter from the real `chat()` calls below).
    """

    def __init__(self, answer_text: str, verdict: str = "GROUNDED") -> None:
        self._answer_text = answer_text
        self._verdict = verdict
        self.calls = 0

    def chat(self, *_a: object, **_kw: object) -> str:
        self.calls += 1
        return self._answer_text if self.calls == 1 else self._verdict

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: object, **_kw: object
    ) -> object:
        return fallback


class _FakeChatStreamSeq:
    """Streaming ``chat_stream()``: one fixed delta sequence per call, in order.

    ``chat_json``: see ``_FakeChatSync`` above -- same reasoning, same always-fallback fake.
    """

    def __init__(self, sequences: list[list[str]]) -> None:
        self._sequences = sequences
        self.calls = 0

    def chat_stream(self, *_a: object, usage: Usage | None = None, **_kw: object) -> Iterator[str]:
        seq = self._sequences[self.calls]
        self.calls += 1
        yield from seq
        if usage is not None:
            usage.add(Usage(prompt_tokens=10, completion_tokens=len(seq)))

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: object, **_kw: object
    ) -> object:
        return fallback


def _timing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "rag_app.timing"]


# --- T13.8.1: atomic-step metrics (secrag_chat_step_seconds), both generation paths -------


def _step_counts() -> dict[tuple[str, str], float]:
    """Current observation counts per (stage, step), read from the REAL process-wide
    ``metrics.REGISTRY`` -- tests below only ever assert a before/after DELTA (a histogram's
    ``_count`` is monotonic), so sharing the registry with the rest of the pytest session is
    harmless, same precedent as ``test_metrics_server_serves_observed_stages_on_its_own_port``."""
    from rag_app import metrics as real_metrics

    out: dict[tuple[str, str], float] = {}
    for metric in real_metrics.REGISTRY.collect():
        if metric.name != "secrag_chat_step_seconds":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                out[(sample.labels["stage"], sample.labels["step"])] = sample.value
    return out


def _stage_sum(stage_name: str) -> float:
    """T13.8.9 (DA-13-G1-fix-1): the REAL cumulative duration (``_sum``, not ``_count``)
    Prometheus has recorded for one ``secrag_chat_stage_seconds`` stage label, across the
    whole pytest session -- ``answer_question`` wires its own internal recorder straight to
    ``metrics.observe_stage``/``observe_step`` (see ``generation.py``), so a before/after
    DELTA of this value is the real stage total for one call, no custom capture needed."""
    from rag_app import metrics as real_metrics

    for metric in real_metrics.REGISTRY.collect():
        if metric.name != "secrag_chat_stage_seconds":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_sum") and sample.labels.get("stage") == stage_name:
                return sample.value
    return 0.0


def _step_sum(stage_name: str, step_name: str) -> float:
    """Same idea as :func:`_stage_sum`, for one ``secrag_chat_step_seconds`` (stage, step)."""
    from rag_app import metrics as real_metrics

    for metric in real_metrics.REGISTRY.collect():
        if metric.name != "secrag_chat_step_seconds":
            continue
        for sample in metric.samples:
            if (
                sample.name.endswith("_sum")
                and sample.labels.get("stage") == stage_name
                and sample.labels.get("step") == step_name
            ):
                return sample.value
    return 0.0


def test_answer_question_emits_the_expected_atomic_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-streaming path: build_prompt/llm_first_token/parse_answer (generating) and
    build_check_prompt/llm_verdict/parse_verdict (checking) -- but never token_stream (no
    incremental stream in this path, T13.8.1's documented mapping choice).

    T13.8.9 (DA-13-G1-fix-1): also asserts the real "generating" step-sum (build_prompt +
    llm_first_token + parse_answer, the generation equivalent of the retrieval/reranking
    real-sum tests) is within a tight tolerance of the real "generate" stage total --
    ``answer_question`` is called for real here (only ``hybrid_search``/the LLM are faked),
    so a regression of the 13-G1-fix (``parse_answer`` moved back OUTSIDE
    ``with timing.stage("generate")``) would fail THIS test, unlike the synthetic
    ``test_metrics.py`` sum test."""
    import time

    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])

    # A deliberate, measurable delay on the REAL parse_answer (module-level name, so the
    # unqualified call inside answer_from_chunks picks it up) -- with trivial fake data every
    # step is otherwise sub-millisecond, so a genuine "this step fell outside the stage span"
    # regression (DA-13-G1-1(c)'s own parse_answer case) would be invisible against timer
    # noise without it.
    real_parse_answer = generation.parse_answer

    def _slow_parse_answer(*args: object, **kwargs: object) -> object:
        time.sleep(0.05)
        return real_parse_answer(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(generation, "parse_answer", _slow_parse_answer)

    chat = _FakeChatSync("Use prepared statements [1].")
    before = _step_counts()
    stage_before = _stage_sum("generate")
    parse_answer_before = _step_sum("generating", "parse_answer")
    steps_before = sum(
        _step_sum("generating", s) for s in ("build_prompt", "llm_first_token", "parse_answer")
    )

    generation.answer_question(None, "q", chat=chat, use_rerank=False)  # type: ignore[arg-type]

    after = _step_counts()
    for pair in (
        ("generating", "build_prompt"),
        ("generating", "llm_first_token"),
        ("generating", "parse_answer"),
        ("checking", "build_check_prompt"),
        ("checking", "llm_verdict"),
        ("checking", "parse_verdict"),
    ):
        assert after.get(pair, 0.0) > before.get(pair, 0.0), pair
    token_stream = ("generating", "token_stream")
    assert after.get(token_stream, 0.0) == before.get(token_stream, 0.0)

    stage_delta = _stage_sum("generate") - stage_before
    steps_delta = (
        sum(_step_sum("generating", s) for s in ("build_prompt", "llm_first_token", "parse_answer"))
        - steps_before
    )
    parse_answer_delta = _step_sum("generating", "parse_answer") - parse_answer_before
    # The injected 0.05s delay makes the stage/step comparison below actually discriminate --
    # with trivial fake data every step would otherwise be sub-millisecond either way.
    assert parse_answer_delta >= 0.04, "the injected 0.05s delay on parse_answer did not land"
    assert steps_delta == pytest.approx(stage_delta, abs=0.02)


def test_answer_question_chitchat_returns_an_llm_reply_without_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14-B2 finding 3 (14-B2-fix): ``answer_question`` (``POST /chat``, non-streaming)
    gained the SAME chit-chat branch the stream path already has (T14.2.1) -- an exact
    greeting gets a grounded, non-abstained reply with NO retrieval call at all, the
    non-streaming mirror of
    ``test_answer_question_stream_chitchat_emits_classify_and_canned_reply_steps``. Fails on
    pre-14-B2 code: ``answer_question`` had no chit-chat branch at all, so "hi" would have
    run full RAG straight into the deliberately-broken ``hybrid_search`` below."""

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("retrieval must never run for a chit-chat turn")

    monkeypatch.setattr(generation, "hybrid_search", _boom)
    chat = _FakeChatSync("")  # empty reply -> falls back to the safe bilingual template

    answer = generation.answer_question(None, "hi", chat=chat, use_rerank=False)  # type: ignore[arg-type]

    assert answer.abstained is False
    assert answer.grounded is True
    assert answer.text  # the non-empty template fallback (empty fake LLM reply)
    assert answer.citations == []
    assert chat.calls == 1, "only the chit-chat reply call should run (no groundedness verdict)"


def test_answer_question_stream_emits_the_expected_atomic_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    monkeypatch.setattr(
        reranking.CrossEncoderReranker, "rerank", lambda _self, _q, chunks, _n: chunks
    )
    chat = _FakeChatStreamSeq([["Use prepared statements ", "[1]."], ["GROUNDED"]])
    before = _step_counts()
    classify_stage_before = _stage_sum("classify")

    list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
        )
    )

    after = _step_counts()
    for pair in (
        ("generating", "build_prompt"),
        ("generating", "llm_first_token"),
        ("generating", "token_stream"),
        ("generating", "parse_answer"),
        ("checking", "build_check_prompt"),
        ("checking", "llm_verdict"),
        ("checking", "parse_verdict"),
    ):
        assert after.get(pair, 0.0) > before.get(pair, 0.0), pair
    # T14.2.1/T14.2.3 (block 14-B2): classify_intent's own step is gone from the live
    # pipeline (replaced by the shared `understand()` -- fast path, then the LLM router);
    # the coarse "classify" STAGE total is still recorded around that call.
    assert _stage_sum("classify") > classify_stage_before


def test_answer_question_stream_chitchat_emits_classify_and_canned_reply_steps() -> None:
    """T14.2.1/T14.2.2 (block 14-B2): the fast path matches "hi" and skips the router --
    only the coarse "classify"/"responding" stage totals and the "stream_canned_reply" step
    are observed, nothing from retrieving/reranking/generating/checking. The reply is now
    LLM-generated (``_FakeChatSync("")``'s empty ``chat()`` reply falls back to the safe
    template, P14-D1), not a hard-coded canned string, but the step/stage NAMES recording the
    word-by-word streaming are unchanged."""
    before = _step_counts()
    classify_stage_before = _stage_sum("classify")
    responding_stage_before = _stage_sum("responding")

    list(generation.answer_question_stream(None, "hi", chat=_FakeChatSync("")))  # type: ignore[arg-type]

    after = _step_counts()
    canned = ("responding", "stream_canned_reply")
    assert after.get(canned, 0.0) > before.get(canned, 0.0)
    assert _stage_sum("classify") > classify_stage_before
    assert _stage_sum("responding") > responding_stage_before
    build_prompt = ("generating", "build_prompt")
    assert after.get(build_prompt, 0.0) == before.get(build_prompt, 0.0)


# --- T14.2.5 (block 14-C1): memory forwarded into understand() -----------------------------


def test_answer_question_stream_forwards_summary_and_last_turn_into_understand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``api.conversations`` reads memory on its own session and passes it here as plain
    ``summary``/``last_turn`` kwargs -- this test proves they reach ``understand()``
    unchanged, without needing a real conversation/DB. Fails on the pre-14-C1 code with a
    ``TypeError`` (``answer_question_stream`` took no such kwargs at all)."""
    calls: list[tuple[str, str | None, str | None]] = []
    real_understand = generation.understand

    def spy(
        chat: object,
        query: str,
        *,
        summary: str | None = None,
        last_turn: str | None = None,
    ) -> object:
        calls.append((query, summary, last_turn))
        return real_understand(  # type: ignore[arg-type]
            chat, query, summary=summary, last_turn=last_turn
        )

    monkeypatch.setattr(generation, "understand", spy)

    list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "hi",
            chat=_FakeChatSync(""),
            summary='{"topics": ["sqli"]}',
            last_turn="User: hello\nAssistant: hi there",
        )
    )

    assert calls == [("hi", '{"topics": ["sqli"]}', "User: hello\nAssistant: hi there")]


def test_answer_question_stream_defaults_memory_to_none() -> None:
    """``POST /chat`` (``answer_question``) never passes memory at all (TF24, no
    conversation); the stream path must still work with the defaults, same as before 14-C1."""
    events = list(generation.answer_question_stream(None, "hi", chat=_FakeChatSync("")))  # type: ignore[arg-type]
    assert any(isinstance(e, StreamResult) for e in events)


# --- T14.2.7 (block 14-C2): retrieval searches with the router's standalone_query -----------


class _FakeChatSyncWithRouterOutput:
    """Like ``_FakeChatSync``, but ``chat_json`` returns a CALLER-SUPPLIED ``RouterOutput``
    instead of always echoing ``fallback`` -- needed to prove retrieval sees a REWRITTEN
    ``standalone_query``, which ``_FakeChatSync``'s always-fallback ``chat_json`` can never
    exercise (its fallback's ``standalone_query`` is always the raw query, by the router's
    own conservative-fallback construction)."""

    def __init__(
        self, router_output: RouterOutput, answer_text: str, verdict: str = "GROUNDED"
    ) -> None:
        self._router_output = router_output
        self._answer_text = answer_text
        self._verdict = verdict
        self.calls = 0

    def chat(self, *_a: object, **_kw: object) -> str:
        self.calls += 1
        return self._answer_text if self.calls == 1 else self._verdict

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: object, **_kw: object
    ) -> object:
        return self._router_output


class _FakeChatStreamSeqWithRouterOutput:
    """Streaming twin of ``_FakeChatSyncWithRouterOutput`` above (mirrors
    ``_FakeChatStreamSeq``'s own ``chat_stream`` shape)."""

    def __init__(self, router_output: RouterOutput, sequences: list[list[str]]) -> None:
        self._router_output = router_output
        self._sequences = sequences
        self.calls = 0

    def chat_stream(self, *_a: object, usage: Usage | None = None, **_kw: object) -> Iterator[str]:
        seq = self._sequences[self.calls]
        self.calls += 1
        yield from seq
        if usage is not None:
            usage.add(Usage(prompt_tokens=10, completion_tokens=len(seq)))

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: object, **_kw: object
    ) -> object:
        return self._router_output


def test_answer_question_retrieves_with_the_standalone_query_not_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14.2.7: a follow-up's raw message means nothing to hybrid search on its own --
    retrieval must search with the router's resolved, self-contained English
    ``standalone_query``. Fails on pre-14-C2 code: ``hybrid_search`` would be called with the
    ORIGINAL message, never the rewrite."""
    router_output = RouterOutput(
        intent="followup",
        language="en",
        standalone_query="How does SQL injection rank in the OWASP Top 10 2021?",
    )
    chat = _FakeChatSyncWithRouterOutput(router_output, "It ranks #3 [1].")
    seen_queries: list[str] = []

    def fake_hybrid_search(_session: object, query: str, **_kw: object) -> list[object]:
        seen_queries.append(query)
        return [_chunk()]

    monkeypatch.setattr(generation, "hybrid_search", fake_hybrid_search)

    generation.answer_question(None, "where does it rank?", chat=chat, use_rerank=False)  # type: ignore[arg-type]

    assert seen_queries == ["How does SQL injection rank in the OWASP Top 10 2021?"]


def test_answer_question_still_generates_with_the_original_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation must still address what the user actually typed, in their own language
    (D14.1) -- the ``standalone_query`` is for retrieval only (T14.2.7)."""
    router_output = RouterOutput(
        intent="followup", language="es", standalone_query="REWRITTEN ENGLISH QUERY"
    )
    chat = _FakeChatSyncWithRouterOutput(router_output, "Queda en el puesto 3 [1].")
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    seen_generation_queries: list[str] = []
    real_build_messages = generation.build_messages

    def spy_build_messages(query: str, chunks: object) -> object:
        seen_generation_queries.append(query)
        return real_build_messages(query, chunks)  # type: ignore[arg-type]

    monkeypatch.setattr(generation, "build_messages", spy_build_messages)

    generation.answer_question(None, "¿y eso, en qué puesto queda?", chat=chat, use_rerank=False)  # type: ignore[arg-type]

    assert seen_generation_queries == ["¿y eso, en qué puesto queda?"]


def test_answer_question_retrieval_falls_back_to_the_original_query_on_router_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router's own conservative fallback (any failure -> security intent, raw query)
    means retrieval still searches with the ORIGINAL message when the router is
    unreachable -- T14.2.7 introduces no new, less-safe behaviour."""

    class _RaisingRouterChat:
        def __init__(self) -> None:
            self.calls = 0

        def chat_json(self, *_a: object, **_kw: object) -> object:
            raise TimeoutError("boom")

        def chat(self, *_a: object, **_kw: object) -> str:
            self.calls += 1
            return "Use prepared statements [1]." if self.calls == 1 else "GROUNDED"

    chat = _RaisingRouterChat()
    seen_queries: list[str] = []

    def fake_hybrid_search(_session: object, query: str, **_kw: object) -> list[object]:
        seen_queries.append(query)
        return [_chunk()]

    monkeypatch.setattr(generation, "hybrid_search", fake_hybrid_search)

    generation.answer_question(
        None,
        "how do I prevent SQL injection?",
        chat=chat,
        use_rerank=False,  # type: ignore[arg-type]
    )

    assert seen_queries == ["how do I prevent SQL injection?"]


def test_answer_question_stream_retrieves_with_the_standalone_query_not_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming twin of
    ``test_answer_question_retrieves_with_the_standalone_query_not_the_original`` -- the
    embed call, the hybrid search and the rerank call must all search with the rewritten
    English ``standalone_query``, not the original message."""
    router_output = RouterOutput(
        intent="followup",
        language="en",
        standalone_query="How does the OS command injection ranking compare?",
    )
    chat = _FakeChatStreamSeqWithRouterOutput(router_output, [["Use ", "[1]."], ["GROUNDED"]])
    seen_queries: list[str] = []

    monkeypatch.setattr(
        generation,
        "embed_query",
        lambda _embedder, query: (seen_queries.append(("embed", query)) or [0.0]),
    )

    def fake_hybrid_search(_session: object, query: str, **_kw: object) -> list[object]:
        seen_queries.append(("hybrid_search", query))
        return [_chunk()]

    monkeypatch.setattr(generation, "hybrid_search", fake_hybrid_search)

    def fake_rerank(_self: object, query: str, chunks: list[object], _top_n: int) -> list[object]:
        seen_queries.append(("rerank", query))
        return chunks

    monkeypatch.setattr(reranking.CrossEncoderReranker, "rerank", fake_rerank)

    list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "what about that one, which rank is it?",
            chat=chat,  # type: ignore[arg-type]
        )
    )

    expected = "How does the OS command injection ranking compare?"
    assert seen_queries == [
        ("embed", expected),
        ("hybrid_search", expected),
        ("rerank", expected),
    ]


def test_answer_question_stream_still_generates_with_the_original_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streamed reply must still address what the user actually typed, in their own
    language (D14.1) -- T14.2.7 only changes what retrieval searches with."""
    router_output = RouterOutput(
        intent="followup", language="es", standalone_query="REWRITTEN ENGLISH QUERY"
    )
    chat = _FakeChatStreamSeqWithRouterOutput(router_output, [["Use ", "[1]."], ["GROUNDED"]])
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])
    monkeypatch.setattr(
        reranking.CrossEncoderReranker, "rerank", lambda _self, _q, chunks, _n: chunks
    )
    seen_generation_queries: list[str] = []
    real_stream_answer_tokens = generation._stream_answer_tokens

    def spy_stream_answer_tokens(
        chat_arg: object, query: str, chunks: object, usage: object
    ) -> object:
        seen_generation_queries.append(query)
        return real_stream_answer_tokens(chat_arg, query, chunks, usage)  # type: ignore[arg-type]

    monkeypatch.setattr(generation, "_stream_answer_tokens", spy_stream_answer_tokens)

    list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "¿y eso, en qué puesto queda?",
            chat=chat,  # type: ignore[arg-type]
        )
    )

    assert seen_generation_queries == ["¿y eso, en qué puesto queda?"]


# --- T13.12.5 (DEBT-062): StreamStep sub-step events (embed_query start/end) ----------------


def test_answer_question_stream_emits_embed_query_start_and_end_exactly_once_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security path brackets the (potentially slow, cold-start) embed call with
    exactly one start and one end ``StreamStep``, in the right place: after the
    "retrieving" stage, before any further stage event."""
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])
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

    steps = [e for e in events if isinstance(e, StreamStep)]
    assert [(s.stage, s.step, s.phase) for s in steps] == [
        ("retrieving", "embed_query", "start"),
        ("retrieving", "embed_query", "end"),
    ]
    # The embed step sits strictly between the "retrieving" stage event and the next
    # ("reranking") stage event -- never before/after the wrong stage.
    kinds = [
        ("stage", e.stage) if isinstance(e, StreamStage) else ("step", e.phase)
        for e in events
        if isinstance(e, StreamStage | StreamStep)
    ]
    retrieving_idx = kinds.index(("stage", "retrieving"))
    reranking_idx = kinds.index(("stage", "reranking"))
    assert kinds[retrieving_idx + 1] == ("step", "start")
    assert kinds[retrieving_idx + 2] == ("step", "end")
    assert retrieving_idx + 2 < reranking_idx


def test_answer_question_stream_chitchat_never_emits_a_step_event() -> None:
    """The chit-chat fast path never touches retrieval/embedding at all -- no StreamStep,
    ever (DA-less regression guard: a future refactor that accidentally always embeds would
    fail this)."""
    events = list(generation.answer_question_stream(None, "hi", chat=_FakeChatSync("")))  # type: ignore[arg-type]
    assert not [e for e in events if isinstance(e, StreamStep)]


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
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])
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
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])
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
