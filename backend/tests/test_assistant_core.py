"""Unit tests for the shared understanding step (T14.2.1, block 14-B2).

No DB/live LLM: ``understand`` is exercised against a fake ``ChatClient``. Every test here
fails on the pre-14-B2 code with an ``ImportError`` (``rag_app.assistant.core`` does not
exist yet).
"""

from __future__ import annotations

from prometheus_client import Counter

from rag_app import metrics
from rag_app.assistant.core import (
    Understanding,
    named_owasp_version,
    retrieval_query,
    understand,
)
from rag_app.assistant.router import RouterOutput


class _FakeChat:
    """``chat_json`` records whether it was ever called (to prove the fast path skips the
    router entirely, T14.2.2) and otherwise returns a canned result."""

    def __init__(self, result: RouterOutput | None = None) -> None:
        self._result = result
        self.chat_json_calls = 0

    def chat_json(
        self, _messages: object, _schema: object, *, fallback: RouterOutput, **_kw: object
    ) -> RouterOutput:
        self.chat_json_calls += 1
        return self._result if self._result is not None else fallback

    def chat(self, *_a: object, **_kw: object) -> str:
        return "a reply"


def test_exact_fast_path_match_skips_the_router_entirely() -> None:
    chat = _FakeChat()
    decision = understand(chat, "hola")  # type: ignore[arg-type]
    assert decision.intent == "chitchat"
    assert decision.language == "es"
    assert decision.used_fast_path is True
    assert decision.router_latency_s is None
    assert chat.chat_json_calls == 0  # the router was never called


def test_non_exact_message_calls_the_router_and_measures_its_latency() -> None:
    """DA-router-reliability-2 (F24-into-14), widened by the 14-D2 ride-along: a raw
    'followup' verdict with no context (no summary/last_turn passed here) is no longer
    trusted -- it now falls through directly to 'security' (the conservative default),
    never 'ambiguous'. This test's own purpose (the router WAS called, and latency WAS
    measured) is unaffected."""
    result = RouterOutput(intent="followup", language="en", standalone_query="x")
    chat = _FakeChat(result=result)
    decision = understand(chat, "what about that?")  # type: ignore[arg-type]
    assert decision.intent == "security"
    assert decision.used_fast_path is False
    assert chat.chat_json_calls == 1
    assert decision.router_latency_s is not None
    assert decision.router_latency_s >= 0.0


def test_a_lone_greeting_typo_is_now_fast_pathed_too() -> None:
    """router-reliability (DEC-066 change 1), supersedes the old PHASE_PLANNING.md 14.2
    wording ("a lone 'hol'/'hell' is not exact -> router"): a lone greeting-typo word now
    skips the router entirely too (``assistant.fast_path.match_fast_path``'s own fuzzy
    match) -- this fixed the golden-set's deterministic ``off_topic`` misses for exactly
    these two words."""
    chat = _FakeChat()
    decision = understand(chat, "hol")  # type: ignore[arg-type]
    assert decision.used_fast_path is True
    assert chat.chat_json_calls == 0  # the router was never called
    assert decision.intent == "chitchat"
    assert decision.language == "es"


def test_a_typo_word_inside_a_sentence_still_reaches_the_router() -> None:
    """The lone-word scope of change 1 does not widen to a sentence -- a security question
    with a typo'd greeting word embedded still goes through the real router, same as before.
    Not in the golden set (DEC-066's own test requirement)."""
    result = RouterOutput(intent="security", language="en", standalone_query="x")
    chat = _FakeChat(result=result)
    decision = understand(chat, "hell, how do I stop XSS?")  # type: ignore[arg-type]
    assert decision.used_fast_path is False
    assert chat.chat_json_calls == 1
    assert decision.intent == "security"


def test_router_failure_still_degrades_to_security_with_a_measured_latency() -> None:
    """The router's own failure handling (route()) already returns the safe fallback without
    raising -- understand() must not swallow that into a crash, and still times the call."""

    class _RaisingChat:
        def chat_json(self, *_a: object, **_kw: object) -> RouterOutput:
            raise TimeoutError("boom")

    decision = understand(_RaisingChat(), "how do I prevent SQL injection?")  # type: ignore[arg-type]
    assert decision.intent == "security"
    assert decision.standalone_query == "how do I prevent SQL injection?"
    assert decision.used_fast_path is False
    assert decision.router_latency_s is not None


# --- T14.5.2: understanding telemetry (intent distribution, rewrite, clarification) --------


def _reset(counter: Counter, intent: str) -> None:
    counter.labels(intent=intent)._value.set(0)


def _value(counter: Counter, intent: str) -> float:
    return counter.labels(intent=intent)._value.get()


def test_understand_records_intent_distribution_for_the_fast_path() -> None:
    """Fails on the old code: before T14.5.2 the fast path never touched any counter."""
    _reset(metrics.UNDERSTANDING_INTENT_TOTAL, "chitchat")
    before = _value(metrics.UNDERSTANDING_INTENT_TOTAL, "chitchat")
    understand(_FakeChat(), "hola")  # type: ignore[arg-type]
    assert _value(metrics.UNDERSTANDING_INTENT_TOTAL, "chitchat") == before + 1.0


def test_understand_records_intent_distribution_for_a_router_call() -> None:
    result = RouterOutput(intent="security", language="en", standalone_query="x")
    _reset(metrics.UNDERSTANDING_INTENT_TOTAL, "security")
    understand(_FakeChat(result=result), "how do I prevent XSS?")  # type: ignore[arg-type]
    assert _value(metrics.UNDERSTANDING_INTENT_TOTAL, "security") == 1.0


def test_understand_records_a_rewrite_when_standalone_query_differs_from_the_message() -> None:
    """Fails on the old code: no rewrite counter existed before T14.5.2."""
    result = RouterOutput(
        intent="followup", language="en", standalone_query="a genuinely different rewrite"
    )
    _reset(metrics.UNDERSTANDING_REWRITE_TOTAL, "security")  # the DA-router-reliability-2
    # guard turns a context-free "followup" into "security" -- see test above.
    understand(_FakeChat(result=result), "what about that?")  # type: ignore[arg-type]
    assert _value(metrics.UNDERSTANDING_REWRITE_TOTAL, "security") == 1.0


def test_understand_never_records_a_rewrite_when_standalone_query_matches_the_message() -> None:
    result = RouterOutput(intent="security", language="en", standalone_query="same message")
    _reset(metrics.UNDERSTANDING_REWRITE_TOTAL, "security")
    understand(_FakeChat(result=result), "same message")  # type: ignore[arg-type]
    assert _value(metrics.UNDERSTANDING_REWRITE_TOTAL, "security") == 0.0


def test_understand_records_a_clarification_only_for_an_ambiguous_verdict() -> None:
    result = RouterOutput(
        intent="ambiguous", language="en", standalone_query="x", clarification="what do you mean?"
    )
    _reset(metrics.UNDERSTANDING_CLARIFICATION_TOTAL, "ambiguous")
    understand(_FakeChat(result=result), "how do I fix this?")  # type: ignore[arg-type]
    assert _value(metrics.UNDERSTANDING_CLARIFICATION_TOTAL, "ambiguous") == 1.0


def test_understand_never_records_a_clarification_for_a_non_ambiguous_verdict() -> None:
    result = RouterOutput(intent="security", language="en", standalone_query="x")
    _reset(metrics.UNDERSTANDING_CLARIFICATION_TOTAL, "ambiguous")
    understand(_FakeChat(result=result), "how do I prevent SQL injection?")  # type: ignore[arg-type]
    assert _value(metrics.UNDERSTANDING_CLARIFICATION_TOTAL, "ambiguous") == 0.0


# --- retrieval_query (T14.2.7, block 14-C2) -------------------------------------------------


def _decision(standalone_query: str) -> Understanding:
    return Understanding(
        intent="followup",
        language="en",
        standalone_query=standalone_query,
        clarification=None,
        used_fast_path=False,
        router_latency_s=0.1,
    )


def test_retrieval_query_uses_the_standalone_query_when_present() -> None:
    """The whole point of T14.2.7: a follow-up's raw message often means nothing to hybrid
    search on its own -- retrieval must search with the router's resolved, self-contained
    English rewrite instead. Fails on pre-14-C2 code: the function does not exist."""
    decision = _decision("How does SQL injection rank in the OWASP Top 10 2021?")
    assert (
        retrieval_query(decision, "where does it rank?")
        == "How does SQL injection rank in the OWASP Top 10 2021?"
    )


def test_retrieval_query_falls_back_to_the_original_when_standalone_query_is_blank() -> None:
    """Defensive guard (no live caller produces this today -- the router's own
    ``_security_fallback``/the exact fast path both already set
    ``standalone_query == query``): an empty rewrite must never make retrieval search with
    nothing."""
    assert retrieval_query(_decision(""), "original query") == "original query"


def test_retrieval_query_falls_back_to_the_original_when_standalone_query_is_whitespace_only() -> (
    None
):
    assert retrieval_query(_decision("   "), "original query") == "original query"


def test_retrieval_query_matches_the_original_on_the_security_fallback_path() -> None:
    """The security-fallback path (any router failure) already sets ``standalone_query ==
    query`` by construction (``router._security_fallback``) -- ``retrieval_query`` must keep
    that identity, not accidentally diverge from it."""
    decision = understand(
        _FakeChat(),  # chat_json raises nothing here; _FakeChat() with no result -> fallback
        "how do I prevent SQL injection?",
    )  # type: ignore[arg-type]
    assert retrieval_query(decision, "how do I prevent SQL injection?") == (
        "how do I prevent SQL injection?"
    )


# --- named_owasp_version (T14.2.9, block 14-C3, DEC-062) -----------------------------------


def test_named_owasp_version_finds_a_known_corpus_edition() -> None:
    """Fails on pre-14-C3 code: the function does not exist."""
    assert named_owasp_version("What rank does SQL injection have in the 2021 OWASP Top 10?") == (
        "2021"
    )
    assert named_owasp_version("What about the 2025 edition?") == "2025"
    assert named_owasp_version("Injection was A1 in the 2017 Top 10") == "2017"


def test_named_owasp_version_is_none_when_no_edition_is_named() -> None:
    """DEC-062's own wording: 'never when not named' -- a plain security question, or one
    naming a year that is not one of the corpus's three editions, must never trigger a
    version filter."""
    assert named_owasp_version("How do I prevent SQL injection?") is None
    assert named_owasp_version("What happened in 2030?") is None  # not a corpus edition


def test_named_owasp_version_ignores_a_year_elsewhere_in_the_text() -> None:
    """A real false-positive guard: the regex is word-bounded, so a longer number containing
    one of the edition digits (e.g. a CVE-style id) must not match."""
    assert named_owasp_version("See CWE-12021 for details") is None
