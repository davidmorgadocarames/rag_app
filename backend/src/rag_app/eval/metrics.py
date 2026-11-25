"""Evaluation metrics — pure aggregation over per-item results.

Retrieval and generation are measured separately, plus correctness against an
independent ground truth (catches the *faithful-but-stale* failure), plus correct
abstention on out-of-corpus questions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ItemResult:
    id: str
    answerable: bool
    retrieved_docs: list[str]
    expected_doc: str | None
    abstained: bool
    grounded: bool
    correct: bool | None  # judge verdict for answerable items; None for negatives


@dataclass
class Metrics:
    n_answerable: int
    n_negative: int
    retrieval_recall: float
    faithfulness: float
    correctness: float
    correct_abstention: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def compute_metrics(results: list[ItemResult]) -> Metrics:
    """Aggregate per-item results into the gate metrics."""
    answerable = [r for r in results if r.answerable]
    negative = [r for r in results if not r.answerable]

    recall_hits = sum(
        1 for r in answerable if r.expected_doc and r.expected_doc in r.retrieved_docs
    )
    answered = [r for r in answerable if not r.abstained]
    faithful_hits = sum(1 for r in answered if r.grounded)
    correct_hits = sum(1 for r in answerable if r.correct is True)
    abstained_hits = sum(1 for r in negative if r.abstained)

    return Metrics(
        n_answerable=len(answerable),
        n_negative=len(negative),
        retrieval_recall=_ratio(recall_hits, len(answerable)),
        faithfulness=_ratio(faithful_hits, len(answered)),
        correctness=_ratio(correct_hits, len(answerable)),
        correct_abstention=_ratio(abstained_hits, len(negative)),
    )


@dataclass
class DialogueTurnResult:
    """One replayed turn of a golden-set v2 dialogue (T14.0.4, block 14-A2; ``observed_intent``
    and ``router_latency_s`` updated for the real router/fast path in block 14-B2).

    ``observed_intent`` is what the pipeline actually decided for this turn --- before 14-B2
    always ``"chitchat"``/``"security"`` (``rag_app.generation.classify_intent``, no router);
    from 14-B2 the real 5-way ``rag_app.assistant.router.RouterOutput.intent`` (or
    ``"chitchat"`` from the exact fast path, which never calls the router) --- a 5-way match of
    ``expected_intent``. A dialogue still has no memory (14-C1): every turn is replayed
    standalone, only its own message (F14.2) --- the router call itself exists now, but
    ``summary``/``last_turn`` are always ``None``.
    """

    dialogue_id: str
    turn_index: int
    expected_intent: str
    observed_intent: str
    is_noisy: bool
    retrieved_docs: list[str]
    expected_doc: str | None
    answerable: bool | None
    abstained: bool
    reply_text: str
    correct: bool | None  # judge verdict for answerable turns; None otherwise
    clarified: bool
    abstention_no_claims: bool | None  # judge verdict when abstained; None otherwise
    latency_s: float  # wall time for this turn's understand+reply/retrieve+answer
    # Wall time of the LLM router call ALONE (block 14-B2); ``None`` when the exact fast path
    # matched (no router call happened to time) -- never 0.0 as a stand-in for "not measured".
    router_latency_s: float | None = None


@dataclass
class DialogueMetrics:
    """Aggregate metrics over a golden-set v2 replay (T14.0.4/T14.0.5).

    Every ratio uses the same ``_ratio`` convention as :class:`Metrics`: an empty denominator
    defaults to 1.0 (no penalty for a category the fixture happens not to exercise) rather than
    0/0. ``router_p95_s`` is ``None`` when no turn in the run ever reached the router (every
    turn matched the exact fast path) rather than a fabricated 0; before block 14-B2 it was
    always ``None`` (no router existed yet).
    """

    n_turns: int
    intent_accuracy: float
    noisy_recall: float
    follow_up_resolution: float
    clarification_rate: float
    conversational_reply_rate: float
    abstention_no_claims_rate: float
    greeting_reply_p95_s: float | None
    router_p95_s: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def looks_like_clarifying_question(reply_text: str, has_citations: bool) -> bool:
    """Heuristic (T14.0.4): a genuine clarifying question is short, ends with "?" and cites
    nothing — today's placeholder never produces one (no such code path exists before T14.3), so
    this always returns False on the "before" baseline; it is written to also recognise the
    real behaviour once 14.3 adds it, instead of being hard-coded to False."""
    text = reply_text.strip()
    return bool(text) and text.endswith("?") and len(text) <= 160 and not has_citations


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def compute_dialogue_metrics(results: list[DialogueTurnResult]) -> DialogueMetrics:
    """Aggregate per-turn dialogue results into the golden-set v2 metrics (pure function, same
    style as :func:`compute_metrics` — every latency/ratio is derived from ``results`` alone)."""
    n_turns = len(results)
    intent_hits = sum(1 for r in results if r.observed_intent == r.expected_intent)

    noisy = [r for r in results if r.is_noisy and r.answerable and r.expected_doc]
    noisy_hits = sum(1 for r in noisy if r.expected_doc in r.retrieved_docs)

    follow_ups = [r for r in results if r.expected_intent == "followup"]
    follow_up_hits = sum(
        1 for r in follow_ups if r.expected_doc and r.expected_doc in r.retrieved_docs
    )

    ambiguous = [r for r in results if r.expected_intent == "ambiguous"]
    clarified_hits = sum(1 for r in ambiguous if r.clarified)

    chitchat_expected = [r for r in results if r.expected_intent == "chitchat"]
    conversational_hits = sum(1 for r in chitchat_expected if r.observed_intent == "chitchat")

    abstained = [r for r in results if r.abstained]
    no_claims_hits = sum(1 for r in abstained if r.abstention_no_claims)

    # "Greeting reply" latency (floor: p95 <= 2s local): the whole turn's wall time whenever
    # the observed intent is chit-chat (fast path OR router-detected) -- before 14-B2 this was
    # always the instant canned-reply path; from 14-B2 it is a real (small) LLM call. "Router
    # p95" (floor: <=1.5s local / <=1s Azure, block 14-B2 onward): ONLY the router call itself
    # (``DialogueTurnResult.router_latency_s``, ``None`` when the exact fast path matched --
    # excluded here, not counted as 0).
    greeting_latencies = [r.latency_s for r in results if r.observed_intent == "chitchat"]
    router_latencies = [r.router_latency_s for r in results if r.router_latency_s is not None]

    return DialogueMetrics(
        n_turns=n_turns,
        intent_accuracy=_ratio(intent_hits, n_turns),
        noisy_recall=_ratio(noisy_hits, len(noisy)),
        follow_up_resolution=_ratio(follow_up_hits, len(follow_ups)),
        clarification_rate=_ratio(clarified_hits, len(ambiguous)),
        conversational_reply_rate=_ratio(conversational_hits, len(chitchat_expected)),
        abstention_no_claims_rate=_ratio(no_claims_hits, len(abstained)),
        greeting_reply_p95_s=_p95(greeting_latencies),
        router_p95_s=_p95(router_latencies),
    )


def cohen_kappa(y_true: list[bool], y_pred: list[bool]) -> float:
    """Cohen's kappa: agreement corrected for chance (used to validate the LLM judge)."""
    n = len(y_true)
    if n == 0:
        return 1.0
    observed = sum(1 for a, b in zip(y_true, y_pred, strict=True) if a == b) / n
    expected = 0.0
    for label in (True, False):
        p_true = sum(1 for a in y_true if a == label) / n
        p_pred = sum(1 for b in y_pred if b == label) / n
        expected += p_true * p_pred
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)
