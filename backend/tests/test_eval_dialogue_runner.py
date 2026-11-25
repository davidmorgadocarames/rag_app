"""Unit tests for the golden-set v2 runner + metrics (T14.0.4/T14.0.5, block 14-A2; updated
for the real shared core/router in block 14-B2; memory read/write in block 14-C1).

No DB/LLM: `compute_dialogue_metrics` is pure aggregation (same style as `test_eval.py`'s
`compute_metrics` tests), `looks_like_clarifying_question`/`parse_no_claims_verdict` are pure
parsers, and the `evaluate_dialogues` integration tests monkeypatch `retrieve` +
`get_shared_reranker` (never a live DB/Ollama) with a fake chat client that now also
implements `chat_json` (T14.2.3's router call, and T14.2.6's memory-write call since 14-C1)
-- same pattern as `test_generation.py`'s `_FakeChatSync`.

Every test in this file imports a name (`DialogueTurnResult`, `compute_dialogue_metrics`,
`evaluate_dialogues`, `judge_no_claims`, `looks_like_clarifying_question`) that does not exist
before block 14-A2 -- they fail on the pre-14-A2 code with an ImportError, not just a wrong
assertion. The `evaluate_dialogues` tests below additionally fail on the pre-14-B2 code (no
`chat_json`/router call at all, so `_FakeChat` never needed one; `observed_intent` was only a
chitchat/security binary) with an ``AttributeError`` or a wrong value; the memory test fails
on the pre-14-C1 code (``understand()`` took no ``summary``/``last_turn`` kwargs at all --
`spy_understand` would record ``None`` for both on every turn, not the first turn's real
``last_turn``) with a wrong value, not an exception (the kwargs have defaults).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import rag_app.assistant.core as assistant_core
from rag_app import generation
from rag_app.assistant.router import RouterOutput
from rag_app.eval import gate
from rag_app.eval.dataset import Dialogue, Turn
from rag_app.eval.judge import parse_no_claims_verdict
from rag_app.eval.metrics import (
    DialogueMetrics,
    DialogueTurnResult,
    compute_dialogue_metrics,
    looks_like_clarifying_question,
)
from rag_app.eval.runner import evaluate_dialogues
from rag_app.retrieval import RetrievedChunk

# --- compute_dialogue_metrics: pure aggregation -------------------------------------------


def _turn(
    *,
    dialogue_id: str = "d",
    expected_intent: str = "security",
    observed_intent: str = "security",
    is_noisy: bool = False,
    retrieved_docs: list[str] | None = None,
    expected_doc: str | None = None,
    answerable: bool | None = True,
    abstained: bool = False,
    reply_text: str = "answer [1]",
    correct: bool | None = None,
    clarified: bool = False,
    abstention_no_claims: bool | None = None,
    latency_s: float = 0.1,
    router_latency_s: float | None = None,
) -> DialogueTurnResult:
    return DialogueTurnResult(
        dialogue_id=dialogue_id,
        turn_index=0,
        expected_intent=expected_intent,
        observed_intent=observed_intent,
        is_noisy=is_noisy,
        retrieved_docs=retrieved_docs or [],
        expected_doc=expected_doc,
        answerable=answerable,
        abstained=abstained,
        reply_text=reply_text,
        correct=correct,
        clarified=clarified,
        abstention_no_claims=abstention_no_claims,
        latency_s=latency_s,
        router_latency_s=router_latency_s,
    )


def test_intent_accuracy_counts_exact_matches_only() -> None:
    results = [
        _turn(expected_intent="chitchat", observed_intent="chitchat"),
        _turn(expected_intent="security", observed_intent="security"),
        # a real 5-way router miss (e.g. a follow-up it classified as plain security)
        _turn(expected_intent="followup", observed_intent="security"),
    ]
    m = compute_dialogue_metrics(results)
    assert m.intent_accuracy == pytest.approx(2 / 3)


def test_noisy_recall_only_counts_noisy_answerable_turns() -> None:
    results = [
        _turn(is_noisy=True, expected_doc="docA", retrieved_docs=["docA"]),
        _turn(is_noisy=True, expected_doc="docB", retrieved_docs=["docX"]),  # miss
        _turn(is_noisy=False, expected_doc="docC", retrieved_docs=[]),  # not noisy, ignored
    ]
    m = compute_dialogue_metrics(results)
    assert m.noisy_recall == pytest.approx(0.5)


def test_noisy_recall_defaults_to_one_when_no_noisy_turns() -> None:
    m = compute_dialogue_metrics([_turn(is_noisy=False)])
    assert m.noisy_recall == 1.0


def test_follow_up_resolution_uses_expected_doc_recall() -> None:
    results = [
        _turn(expected_intent="followup", expected_doc="docA", retrieved_docs=["docA"]),
        _turn(expected_intent="followup", expected_doc="docB", retrieved_docs=[]),  # miss
        _turn(expected_intent="security", expected_doc="docC", retrieved_docs=["docC"]),
    ]
    m = compute_dialogue_metrics(results)
    assert m.follow_up_resolution == pytest.approx(0.5)


def test_clarification_rate_only_counts_ambiguous_turns() -> None:
    results = [
        _turn(expected_intent="ambiguous", clarified=True),
        _turn(expected_intent="ambiguous", clarified=False),
        _turn(expected_intent="security", clarified=True),  # ignored: not ambiguous
    ]
    m = compute_dialogue_metrics(results)
    assert m.clarification_rate == pytest.approx(0.5)


def test_conversational_reply_rate_checks_chitchat_expected_turns_took_the_fast_path() -> None:
    results = [
        _turn(expected_intent="chitchat", observed_intent="chitchat"),
        _turn(expected_intent="chitchat", observed_intent="security"),  # misclassified
    ]
    m = compute_dialogue_metrics(results)
    assert m.conversational_reply_rate == pytest.approx(0.5)


def test_abstention_no_claims_rate_only_counts_abstained_turns() -> None:
    results = [
        _turn(abstained=True, abstention_no_claims=True),
        _turn(abstained=True, abstention_no_claims=False),
        _turn(abstained=False, abstention_no_claims=None),  # not abstained, ignored
    ]
    m = compute_dialogue_metrics(results)
    assert m.abstention_no_claims_rate == pytest.approx(0.5)


def test_greeting_reply_p95_is_measured_from_chitchat_turns_only() -> None:
    results = [
        _turn(observed_intent="chitchat", latency_s=0.5),
        _turn(observed_intent="chitchat", latency_s=1.5),
        _turn(observed_intent="security", latency_s=99.0),  # excluded
    ]
    m = compute_dialogue_metrics(results)
    assert m.greeting_reply_p95_s == pytest.approx(1.5)


def test_router_p95_is_none_when_no_turn_carries_a_router_latency() -> None:
    """Every turn's fast path matched (no router call happened to time) -- `None`, never a
    fabricated 0."""
    m = compute_dialogue_metrics([_turn(router_latency_s=None), _turn(router_latency_s=None)])
    assert m.router_p95_s is None


def test_router_p95_is_measured_from_turns_with_a_router_latency_only() -> None:
    """block 14-B2: the router now exists, so `router_latency_s` is populated for turns that
    actually called it; a fast-path turn's `None` must be EXCLUDED, not treated as 0 (which
    would silently drag the p95 down)."""
    results = [
        _turn(router_latency_s=0.2),
        _turn(router_latency_s=0.8),
        _turn(router_latency_s=None),  # fast path -- excluded, not a 0.0
    ]
    m = compute_dialogue_metrics(results)
    assert m.router_p95_s == pytest.approx(0.8)


# --- looks_like_clarifying_question: heuristic ---------------------------------------------


def test_looks_like_clarifying_question_positive() -> None:
    assert looks_like_clarifying_question("Which topic do you mean?", False) is True


@pytest.mark.parametrize(
    ("text", "has_citations"),
    [
        ("I don't have a reliable source for that in the corpus.", False),  # no "?"
        ("Use parameterized queries [1].", True),  # a cited RAG answer, not a clarification
        ("x" * 200 + "?", False),  # too long to be a short clarifying question
    ],
)
def test_looks_like_clarifying_question_negative(text: str, has_citations: bool) -> None:
    assert looks_like_clarifying_question(text, has_citations) is False


# --- judge.parse_no_claims_verdict: pure parser ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NO_CLAIMS", True),
        ("CLAIMS", False),
        ("The text makes no claims, so NO_CLAIMS.", True),
        ("unparseable garbage", False),  # fail safe: cannot confirm it is claim-free
    ],
)
def test_parse_no_claims_verdict(raw: str, expected: bool) -> None:
    assert parse_no_claims_verdict(raw) is expected


# --- evaluate_dialogues: integration without a live DB/LLM ----------------------------------


class _FakeChat:
    """First `chat()` call answers, second is the groundedness verdict (mirrors
    `test_generation.py::_FakeChatSync`). `chat_json` (T14.2.3's router call, block 14-B2)
    always returns the caller's `fallback` -- same minimal fake as
    `test_llm_bounds.py::_RecordingChat`, which is enough here: every non-fast-path message in
    this file is plain English/Spanish security text, so the router's conservative
    `fallback` (intent "security", the raw query) IS the behaviour under test."""

    def __init__(self, answer_text: str, verdict: str = "GROUNDED") -> None:
        self._answer_text = answer_text
        self._verdict = verdict
        self.calls = 0

    def chat(self, *_a: object, **_kw: object) -> str:
        self.calls += 1
        return self._answer_text if self.calls == 1 else self._verdict

    def chat_stream(self, *_a: object, **_kw: object) -> Iterator[str]:
        raise NotImplementedError("evaluate_dialogues never streams")

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: object, **_kw: object
    ) -> object:
        return fallback


def _chunk(chunk_uid: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid=chunk_uid, heading="h", text="t", version="2021", effective_date=None, score=1.0
    )


def test_evaluate_dialogues_passes_memory_into_understand_but_not_into_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14.2.5/T14.2.6 (block 14-C1), supersedes the old (pre-14-C1)
    ``test_evaluate_dialogues_has_no_memory_between_turns``, whose premise ("no prior-turn
    context passed to the router") is no longer true: the SECOND turn's ``understand()`` call
    now receives the FIRST turn's real memory (``last_turn`` always; ``summary`` stays
    ``None`` here because ``_FakeChat.chat_json`` always returns the caller's ``fallback`` --
    an empty ``ConversationSummary()`` -- which ``summarize_turn`` correctly treats as
    "dropped", same as a real invalid/unreachable reply would be). Retrieval now searches
    with ``retrieval_query(decision, message)`` (T14.2.7, block 14-C2) -- this fake's
    ``chat_json`` always returns the caller's ``fallback``, whose ``standalone_query`` IS the
    raw message by construction (``router._security_fallback``), so retrieval here still
    sees exactly the raw message, same values as before -- proven, as before, by asserting
    every call to the fake `retrieve` only ever sees the CURRENT turn's own message, never a
    prior one or the memory. See
    ``test_evaluate_dialogues_retrieves_with_the_standalone_query_not_the_raw_message`` below
    for the case where the router DOES rewrite the query."""
    seen_queries: list[str] = []
    understand_calls: list[tuple[str, str | None, str | None]] = []
    real_understand = assistant_core.understand

    def spy_understand(
        chat: object, query: str, *, summary: str | None = None, last_turn: str | None = None
    ) -> object:
        understand_calls.append((query, summary, last_turn))
        return real_understand(chat, query, summary=summary, last_turn=last_turn)  # type: ignore[arg-type]

    def fake_retrieve(
        _session: object, query: str, *, reranker: object, top_n: int | None = None, **_kw: object
    ) -> list[RetrievedChunk]:
        seen_queries.append(query)
        return [_chunk("docA::1")]

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fake_retrieve)
    monkeypatch.setattr(assistant_core, "understand", spy_understand)

    dialogue = Dialogue(
        id="dlg-1",
        turns=(
            Turn(
                message="How do I prevent SQL injection?",
                expected_intent="security",
                language="en",
                expected_doc="docA",
                answerable=True,
                ground_truth="truth",
            ),
            Turn(
                message="What about OS commands?",
                expected_intent="followup",
                language="en",
                expected_doc="docA",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )

    chat = _FakeChat("Use parameterized queries [1].")
    results = evaluate_dialogues(None, [dialogue], chat=chat, top_n=3)  # type: ignore[arg-type]

    assert seen_queries == ["How do I prevent SQL injection?", "What about OS commands?"]
    assert understand_calls[0] == ("How do I prevent SQL injection?", None, None)
    assert understand_calls[1] == (
        "What about OS commands?",
        None,  # dropped: _FakeChat.chat_json always returns the empty fallback summary
        "User: How do I prevent SQL injection?\nAssistant: Use parameterized queries [1].",
    )
    assert [r.observed_intent for r in results] == ["security", "security"]
    assert all(r.expected_doc == "docA" and r.retrieved_docs == ["docA"] for r in results)


def test_evaluate_dialogues_takes_the_chitchat_fast_path_without_retrieving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_retrieve(*_a: object, **_kw: object) -> list[RetrievedChunk]:
        raise AssertionError("chit-chat must never call retrieve")

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fail_retrieve)

    dialogue = Dialogue(
        id="chat-1", turns=(Turn(message="Thanks!", expected_intent="chitchat", language="en"),)
    )
    results = evaluate_dialogues(None, [dialogue], chat=_FakeChat("unused"))  # type: ignore[arg-type]

    assert len(results) == 1
    assert results[0].observed_intent == "chitchat"
    assert results[0].abstained is False
    assert results[0].retrieved_docs == []


def test_evaluate_dialogues_skips_the_memory_write_on_a_fast_path_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14-C1 finding 1 (HIGH), 14-C1-fix: this runner wires the identical pattern as
    ``/chat/stream`` -- a fast-path chit-chat turn must not cost the extra bounded
    ``summarize_turn`` call either. Fails on 9b6bb2e: ``summarize_turn`` there is called
    unconditionally after every turn, so ``calls`` would be ``[{...}]``, not ``[]``."""

    def fail_retrieve(*_a: object, **_kw: object) -> list[RetrievedChunk]:
        raise AssertionError("chit-chat must never call retrieve")

    calls: list[dict[str, object]] = []

    def fake_summarize_turn(*_a: object, **kw: object) -> str:
        calls.append(kw)
        return '{"topics": ["should never be reached"]}'

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fail_retrieve)
    monkeypatch.setattr("rag_app.assistant.memory.summarize_turn", fake_summarize_turn)

    dialogue = Dialogue(
        id="chat-1", turns=(Turn(message="Thanks!", expected_intent="chitchat", language="en"),)
    )
    results = evaluate_dialogues(None, [dialogue], chat=_FakeChat("unused"))  # type: ignore[arg-type]

    assert len(results) == 1
    assert results[0].observed_intent == "chitchat"
    assert calls == []


def test_evaluate_dialogues_noisy_flag_follows_the_typo_id_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr(
        "rag_app.reranking.retrieve", lambda *_a, **_kw: [_chunk("cs-sql-injection-prevention::1")]
    )

    noisy = Dialogue(
        id="typo-hell-midsentence",
        turns=(
            Turn(
                message="what the hell is sql injection",
                expected_intent="security",
                language="en",
                expected_doc="cs-sql-injection-prevention",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )
    plain = Dialogue(
        id="es-sqli-prevent",
        turns=(
            Turn(
                message="como prevenir inyeccion sql",
                expected_intent="security",
                language="es",
                expected_doc="cs-sql-injection-prevention",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )
    results = evaluate_dialogues(  # type: ignore[arg-type]
        None, [noisy, plain], chat=_FakeChat("Use parameterized queries [1].")
    )
    assert results[0].is_noisy is True
    assert results[1].is_noisy is False


# --- T14.2.7 (block 14-C2): retrieval searches with the router's standalone_query ----------


class _FakeChatWithRouterOutput:
    """Like ``_FakeChat`` above, but ``chat_json`` returns a CALLER-SUPPLIED
    ``RouterOutput`` instead of always echoing ``fallback`` -- needed to prove retrieval
    actually sees a REWRITTEN ``standalone_query``, which the always-fallback ``_FakeChat``
    can never exercise (its fallback's ``standalone_query`` is always the raw message, by
    the router's own conservative-fallback construction)."""

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

    def chat_stream(self, *_a: object, **_kw: object) -> Iterator[str]:
        raise NotImplementedError("evaluate_dialogues never streams")

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: object, **_kw: object
    ) -> object:
        return self._router_output


def test_evaluate_dialogues_retrieves_with_the_standalone_query_not_the_raw_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14.2.7: a follow-up's raw message ("What about OS commands, which rank is it?")
    means little to hybrid search on its own -- retrieval must search with the router's
    resolved, self-contained English ``standalone_query``. Fails on pre-14-C2 code:
    ``retrieve`` would be called with the raw ``turn.message``, never the rewrite."""
    router_output = RouterOutput(
        intent="followup",
        language="en",
        standalone_query="How does the OS command injection ranking compare in OWASP Top 10?",
    )
    seen_queries: list[str] = []

    def fake_retrieve(
        _session: object, query: str, *, reranker: object, top_n: int | None = None, **_kw: object
    ) -> list[RetrievedChunk]:
        seen_queries.append(query)
        return [_chunk("docA::1")]

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fake_retrieve)

    dialogue = Dialogue(
        id="dlg-followup",
        turns=(
            Turn(
                message="what about that one, which rank is it?",
                expected_intent="followup",
                language="en",
                expected_doc="docA",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )
    chat = _FakeChatWithRouterOutput(router_output, "It ranks #5 [1].")

    evaluate_dialogues(None, [dialogue], chat=chat)  # type: ignore[arg-type]

    assert seen_queries == ["How does the OS command injection ranking compare in OWASP Top 10?"]


def test_evaluate_dialogues_still_generates_the_answer_from_the_raw_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation must still address what the user actually typed (D14.1) -- the
    ``standalone_query`` is for retrieval only (T14.2.7). Fails if a future change
    accidentally feeds the rewritten English query into ``answer_from_chunks`` too."""
    router_output = RouterOutput(
        intent="followup", language="es", standalone_query="REWRITTEN ENGLISH QUERY"
    )
    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", lambda *_a, **_kw: [_chunk("docA::1")])
    seen_generation_queries: list[str] = []
    real_build_messages = generation.build_messages

    def spy_build_messages(query: str, chunks: object) -> object:
        seen_generation_queries.append(query)
        return real_build_messages(query, chunks)  # type: ignore[arg-type]

    monkeypatch.setattr(generation, "build_messages", spy_build_messages)

    dialogue = Dialogue(
        id="dlg-followup-es",
        turns=(
            Turn(
                message="¿y eso, en qué puesto queda?",
                expected_intent="followup",
                language="es",
                expected_doc="docA",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )
    chat = _FakeChatWithRouterOutput(router_output, "Queda en el puesto 5 [1].")

    evaluate_dialogues(None, [dialogue], chat=chat)  # type: ignore[arg-type]

    assert seen_generation_queries == ["¿y eso, en qué puesto queda?"]


# --- T14.2.9 (block 14-C3, DEC-062): named-edition version filter, third entry point -------


def test_evaluate_dialogues_scopes_retrieval_to_a_named_owasp_edition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEC-062 option A, the THIRD entry point for the named-edition filter (same rule as
    ``generation.answer_question``). Fails on pre-14-C3 code: ``version`` was hardcoded
    ``None`` in ``evaluate_dialogues``."""
    router_output = RouterOutput(
        intent="followup",
        language="en",
        standalone_query="What rank does SQL injection have in the 2021 OWASP Top 10?",
    )
    seen_versions: list[str | None] = []

    def fake_retrieve(
        _session: object,
        _query: str,
        *,
        reranker: object,
        top_n: int | None = None,
        version: str | None = None,
        **_kw: object,
    ) -> list[RetrievedChunk]:
        seen_versions.append(version)
        return [_chunk("docA::1")]

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fake_retrieve)

    dialogue = Dialogue(
        id="dlg-followup-version",
        turns=(
            Turn(
                message="where does it rank?",
                expected_intent="followup",
                language="en",
                expected_doc="docA",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )
    chat = _FakeChatWithRouterOutput(router_output, "It's A03 [1].")

    evaluate_dialogues(None, [dialogue], chat=chat)  # type: ignore[arg-type]

    assert seen_versions == ["2021"]


def test_evaluate_dialogues_never_scopes_retrieval_when_no_edition_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_output = RouterOutput(
        intent="security", language="en", standalone_query="How do I prevent SQL injection?"
    )
    seen_versions: list[str | None] = []

    def fake_retrieve(
        _session: object,
        _query: str,
        *,
        reranker: object,
        top_n: int | None = None,
        version: str | None = None,
        **_kw: object,
    ) -> list[RetrievedChunk]:
        seen_versions.append(version)
        return [_chunk("docA::1")]

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fake_retrieve)

    dialogue = Dialogue(
        id="dlg-security",
        turns=(
            Turn(
                message="how do I prevent SQL injection?",
                expected_intent="security",
                language="en",
                expected_doc="docA",
                answerable=True,
                ground_truth="truth",
            ),
        ),
    )
    chat = _FakeChatWithRouterOutput(router_output, "Use prepared statements [1].")

    evaluate_dialogues(None, [dialogue], chat=chat)  # type: ignore[arg-type]

    assert seen_versions == [None]


# --- gate._check_v2: PER-METRIC floor enforcement (T14.2.8, block 14-C2) -------------------


def _v2_metrics(*, intent_accuracy: float) -> DialogueMetrics:
    return DialogueMetrics(
        n_turns=1,
        intent_accuracy=intent_accuracy,
        noisy_recall=1.0,
        follow_up_resolution=1.0,
        clarification_rate=1.0,
        conversational_reply_rate=1.0,
        abstention_no_claims_rate=1.0,
        greeting_reply_p95_s=0.1,
        router_p95_s=0.1,
    )


def test_check_v2_never_fails_a_metric_whose_own_enforce_flag_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14.2.8 supersedes the old single global `enforce_now` boolean (14-A2) with a
    per-metric `enforce` map -- a metric with `enforce.<key>: false` is recorded (printed as
    "below floor") but can never fail the gate, independently of every other metric."""
    monkeypatch.setattr(
        gate,
        "_load_json_any",
        lambda _path: {"intent_accuracy": 0.90, "enforce": {"intent_accuracy": False}},
    )
    info, failures = gate._check_v2(_v2_metrics(intent_accuracy=0.10))  # far below the floor
    assert failures == []
    assert any("below floor" in line for line in info)


def test_check_v2_fails_a_metric_whose_own_enforce_flag_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for the test above: the SAME shortfall DOES fail once THAT metric's
    own `enforce` flag is turned on, proving the per-metric map genuinely gates the failure
    path, not just decorative."""
    monkeypatch.setattr(
        gate,
        "_load_json_any",
        lambda _path: {"intent_accuracy": 0.90, "enforce": {"intent_accuracy": True}},
    )
    _info, failures = gate._check_v2(_v2_metrics(intent_accuracy=0.10))
    assert len(failures) == 1
    assert "intent_accuracy" in failures[0]


def test_check_v2_enforcement_is_independent_per_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """A metric NOT in the `enforce` map at all defaults to unenforced, even while a
    DIFFERENT metric's floor IS enforced and genuinely met -- proves the per-metric map does
    not accidentally couple metrics together (e.g. one global flag flipping everything)."""
    monkeypatch.setattr(
        gate,
        "_load_json_any",
        lambda _path: {
            "intent_accuracy": 0.90,
            "noisy_recall": 0.90,
            "enforce": {"noisy_recall": True},  # intent_accuracy absent -> defaults to False
        },
    )
    _info, failures = gate._check_v2(_v2_metrics(intent_accuracy=0.10))
    assert failures == []  # noisy_recall (1.0) clears its own enforced floor; intent is unenforced


def test_check_v2_checks_router_p95_s_against_its_own_local_floor_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """14-A2's floors file names the router floor `router_p95_s_local` (a separate key from
    the metric field `router_p95_s`, since local/Azure have different floors) -- T14.2.8 wires
    this check in now that a real router exists to measure (it was a no-op before, by
    construction: 14-A2/14-B2/14-C1 never had a `router_p95_s` value to compare)."""
    monkeypatch.setattr(
        gate,
        "_load_json_any",
        lambda _path: {"router_p95_s_local": 1.5, "enforce": {"router_p95_s": True}},
    )
    metrics = _v2_metrics(intent_accuracy=1.0)
    metrics.router_p95_s = 2.0  # above the 1.5s local floor
    _info, failures = gate._check_v2(metrics)
    assert len(failures) == 1
    assert "router_p95_s" in failures[0]
