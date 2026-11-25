"""Unit tests for the golden-set v2 runner + metrics (T14.0.4/T14.0.5, block 14-A2).

No DB/LLM: `compute_dialogue_metrics` is pure aggregation (same style as `test_eval.py`'s
`compute_metrics` tests), `looks_like_clarifying_question`/`parse_no_claims_verdict` are pure
parsers, and the one `evaluate_dialogues` integration test monkeypatches `retrieve` +
`get_shared_reranker` (never a live DB/Ollama) with a fake chat client, same pattern as
`test_generation.py`'s `_FakeChatSync`.

Every test in this file imports a name (`DialogueTurnResult`, `compute_dialogue_metrics`,
`evaluate_dialogues`, `judge_no_claims`, `looks_like_clarifying_question`) that does not exist
before this block — they fail on the pre-14-A2 code with an ImportError, not just a wrong
assertion.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

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
    )


def test_intent_accuracy_counts_exact_matches_only() -> None:
    results = [
        _turn(expected_intent="chitchat", observed_intent="chitchat"),
        _turn(expected_intent="security", observed_intent="security"),
        # the placeholder core has no "followup" label -> always a miss today
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


def test_router_p95_is_always_none_before_the_router_exists() -> None:
    m = compute_dialogue_metrics([_turn()])
    assert m.router_p95_s is None


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
    `test_generation.py::_FakeChatSync`)."""

    def __init__(self, answer_text: str, verdict: str = "GROUNDED") -> None:
        self._answer_text = answer_text
        self._verdict = verdict
        self.calls = 0

    def chat(self, *_a: object, **_kw: object) -> str:
        self.calls += 1
        return self._answer_text if self.calls == 1 else self._verdict

    def chat_stream(self, *_a: object, **_kw: object) -> Iterator[str]:
        raise NotImplementedError("evaluate_dialogues never streams")


def _chunk(chunk_uid: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid=chunk_uid, heading="h", text="t", version="2021", effective_date=None, score=1.0
    )


def test_evaluate_dialogues_has_no_memory_between_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A follow-up turn is replayed STANDALONE (no prior-turn context passed to retrieve) —
    the placeholder core's defining limitation (F14.2), proven here by asserting every call to
    the fake `retrieve` only ever sees the CURRENT turn's own message, never a prior one."""
    seen_queries: list[str] = []

    def fake_retrieve(
        _session: object, query: str, *, reranker: object, top_n: int | None = None, **_kw: object
    ) -> list[RetrievedChunk]:
        seen_queries.append(query)
        return [_chunk("docA::1")]

    monkeypatch.setattr("rag_app.reranking.get_shared_reranker", lambda: object())
    monkeypatch.setattr("rag_app.reranking.retrieve", fake_retrieve)

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


# --- gate._check_v2: floor-enforcement decision (recorded, not enforced today) -------------


def test_check_v2_never_fails_when_enforce_now_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate,
        "_load_json_any",
        lambda _path: {"intent_accuracy": 0.90, "enforce_now": False},
    )
    metrics = DialogueMetrics(
        n_turns=1,
        intent_accuracy=0.10,  # far below the floor
        noisy_recall=1.0,
        follow_up_resolution=1.0,
        clarification_rate=1.0,
        conversational_reply_rate=1.0,
        abstention_no_claims_rate=1.0,
        greeting_reply_p95_s=0.1,
        router_p95_s=None,
    )
    info, failures = gate._check_v2(metrics)
    assert failures == []
    assert any("below floor" in line for line in info)


def test_check_v2_fails_when_enforce_now_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control for the test above: the same shortfall DOES fail once enforcement is
    turned on, proving `enforce_now` genuinely gates the failure path, not just decorative."""
    monkeypatch.setattr(
        gate,
        "_load_json_any",
        lambda _path: {"intent_accuracy": 0.90, "enforce_now": True},
    )
    metrics = DialogueMetrics(
        n_turns=1,
        intent_accuracy=0.10,
        noisy_recall=1.0,
        follow_up_resolution=1.0,
        clarification_rate=1.0,
        conversational_reply_rate=1.0,
        abstention_no_claims_rate=1.0,
        greeting_reply_p95_s=0.1,
        router_p95_s=None,
    )
    _info, failures = gate._check_v2(metrics)
    assert len(failures) == 1
    assert "intent_accuracy" in failures[0]
