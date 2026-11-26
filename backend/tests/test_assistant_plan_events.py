"""T14.4.1/T14.4.3 (block 14-D2): the chat UI's visible "plan" panel reuses the existing
``StreamStep``/SSE contract (13-J3b, DEBT-062) for four new events --
``understood``/``searching``/``found_sources``/``writing_answer``. Every test here fails on
pre-14-D2 code: the new ``step`` names/``detail`` field do not exist yet (either an
``AttributeError``/``TypeError`` on ``StreamStep(..., detail=...)``, or the event is simply
never yielded).

**Canary (T14.4.3):** the ``detail`` string on EVERY plan-panel ``StreamStep``, across every
branch of ``answer_question_stream`` (chitchat/ambiguous/off_topic/security), must never
contain a fragment of a system prompt, a delimiter marker, or any other non-user-derived
prompt text -- only already-classified/derived values the UI is allowed to see.
"""

from __future__ import annotations

from collections.abc import Iterator

from rag_app import generation
from rag_app.assistant.memory import _MEMORY_SYSTEM
from rag_app.assistant.router import _ROUTER_SYSTEM, RouterOutput
from rag_app.generation import StreamStep
from rag_app.llm import Usage
from rag_app.retrieval import RetrievedChunk


def _chunk(uid: str = "c1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid=uid, heading="h", text="t", version="2021", effective_date=None, score=1.0
    )


class _FakeChatSync:
    """Non-streaming ``chat()``: first call answers, second is the groundedness verdict.
    ``chat_json`` always returns the caller's fallback (conservative "security" intent) --
    same minimal fake as ``test_generation.py``'s own."""

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
    def __init__(
        self, sequences: list[list[str]], router_output: RouterOutput | None = None
    ) -> None:
        self._sequences = sequences
        self._router_output = router_output
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
        return self._router_output if self._router_output is not None else fallback


# --- T14.4.1: the four new events, in order, with the right `detail` text ------------------


def test_plan_events_on_the_security_path_in_order_with_expected_detail(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])  # type: ignore[attr-defined]
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])  # type: ignore[attr-defined]
    chat = _FakeChatStreamSeq([["Use prepared statements ", "[1]."], ["GROUNDED"]])

    query = "What rank does SQL injection have in the 2021 OWASP Top 10?"
    events = list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            query,
            chat=chat,  # type: ignore[arg-type]
            use_rerank=False,
        )
    )

    steps = [e for e in events if isinstance(e, StreamStep)]
    plan_steps = [
        s for s in steps if s.step in ("understood", "searching", "found_sources", "writing_answer")
    ]
    assert [s.step for s in plan_steps] == [
        "understood",
        "searching",
        "found_sources",
        "writing_answer",
    ]
    assert plan_steps[0].detail == "security/en"
    assert plan_steps[1].detail == f"{query} (2021)"
    assert plan_steps[2].detail == "1"
    assert plan_steps[3].detail is None


def test_plan_events_found_sources_reports_zero_on_abstention(monkeypatch: object) -> None:
    """Even the no-chunks abstention path reports a real ("0") outcome, never silence."""
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])  # type: ignore[attr-defined]
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [])  # type: ignore[attr-defined]
    chat = _FakeChatStreamSeq([["No reliable source."]])

    events = list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
            use_rerank=False,
        )
    )

    steps = [e for e in events if isinstance(e, StreamStep)]
    found = [s for s in steps if s.step == "found_sources"]
    assert len(found) == 1
    assert found[0].detail == "0"
    # "Writing answer" never fires -- the abstention short-circuits before "generating".
    assert not [s for s in steps if s.step == "writing_answer"]


def test_plan_events_searching_has_no_edition_suffix_when_none_detected(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])  # type: ignore[attr-defined]
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])  # type: ignore[attr-defined]
    chat = _FakeChatStreamSeq([["Use prepared statements ", "[1]."], ["GROUNDED"]])

    events = list(
        generation.answer_question_stream(
            None,  # type: ignore[arg-type]
            "How do I prevent SQL injection?",
            chat=chat,  # type: ignore[arg-type]
            use_rerank=False,
        )
    )

    searching = next(e for e in events if isinstance(e, StreamStep) and e.step == "searching")
    assert "(" not in searching.detail  # no OWASP edition named in this query


def test_plan_events_understood_fires_for_chitchat_with_no_further_plan_steps() -> None:
    events = list(generation.answer_question_stream(None, "hi", chat=_FakeChatSync("")))  # type: ignore[arg-type]
    steps = [e for e in events if isinstance(e, StreamStep)]
    assert [(s.step, s.detail) for s in steps] == [("understood", "chitchat/en")]


def test_plan_events_understood_fires_for_ambiguous_with_no_further_plan_steps() -> None:
    result = RouterOutput(
        intent="ambiguous",
        language="en",
        standalone_query="How do I fix this?",
        clarification="What exactly would you like help fixing?",
    )
    chat = _FakeChatStreamSeq([["unused"]], router_output=result)
    events = list(
        generation.answer_question_stream(None, "How do I fix this?", chat=chat)  # type: ignore[arg-type]
    )
    steps = [e for e in events if isinstance(e, StreamStep)]
    assert [(s.step, s.detail) for s in steps] == [("understood", "ambiguous/en")]


# --- T14.4.3: canary -- no prompt/system/delimiter text ever leaks into a step's `detail` ---


_FORBIDDEN_FRAGMENTS = [
    "<<<",
    ">>>",
    "UNDERSTANDING stage",
    "UNTRUSTED DATA",
    "RUNNING SUMMARY",
    "Never follow any instruction",
]


def test_router_and_memory_system_prompts_actually_contain_the_forbidden_fragments() -> None:
    """Sanity check for the canary itself: if these fragments ever stop appearing in the
    real system prompts (a future reword), this test fails LOUDLY instead of the canary
    below silently testing nothing."""
    assert "UNDERSTANDING stage" in _ROUTER_SYSTEM
    assert "UNTRUSTED DATA" in _ROUTER_SYSTEM
    assert "<<<" in _ROUTER_SYSTEM
    assert "RUNNING SUMMARY" in _MEMORY_SYSTEM


def test_canary_no_plan_step_detail_ever_contains_a_system_prompt_fragment(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(generation, "embed_query", lambda *_a, **_k: [0.0])  # type: ignore[attr-defined]
    monkeypatch.setattr(generation, "hybrid_search", lambda *_a, **_k: [_chunk()])  # type: ignore[attr-defined]

    branches: list[tuple[str, object]] = [
        ("hi", _FakeChatSync("")),
        (
            "How do I prevent SQL injection?",
            _FakeChatStreamSeq([["Use prepared statements ", "[1]."], ["GROUNDED"]]),
        ),
        (
            "How do I fix this?",
            _FakeChatStreamSeq(
                [["unused"]],
                router_output=RouterOutput(
                    intent="ambiguous",
                    language="en",
                    standalone_query="How do I fix this?",
                    clarification="What exactly would you like help fixing?",
                ),
            ),
        ),
        (
            "Can you write me a poem?",
            _FakeChatStreamSeq(
                [["I can only help with security topics."]],
                router_output=RouterOutput(
                    intent="off_topic", language="en", standalone_query="write me a poem"
                ),
            ),
        ),
    ]

    all_details: list[str] = []
    for query, chat in branches:
        events = list(
            generation.answer_question_stream(None, query, chat=chat, use_rerank=False)  # type: ignore[arg-type]
        )
        all_details.extend(
            e.detail for e in events if isinstance(e, StreamStep) and e.detail is not None
        )

    assert all_details  # the canary must actually have something to check
    for detail in all_details:
        for fragment in _FORBIDDEN_FRAGMENTS:
            assert fragment not in detail, f"leaked prompt fragment {fragment!r} in {detail!r}"
