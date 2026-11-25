"""Run the RAG pipeline over the golden set and collect per-item results."""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from rag_app.config import get_settings
from rag_app.eval.dataset import Dialogue, GoldenItem
from rag_app.eval.metrics import DialogueTurnResult, ItemResult, looks_like_clarifying_question
from rag_app.llm import ChatClient, OllamaChat, make_chat_client


def _doc_slug(chunk_uid: str) -> str:
    return chunk_uid.rsplit("::", 1)[0]


def evaluate(
    session: Session,
    items: list[GoldenItem],
    *,
    chat: OllamaChat | None = None,
    top_n: int | None = None,
) -> list[ItemResult]:
    """Evaluate each golden item end-to-end (retrieve -> rerank -> answer -> judge)."""
    from rag_app.eval.judge import judge_correctness
    from rag_app.generation import answer_from_chunks
    from rag_app.reranking import get_shared_reranker, retrieve

    settings = get_settings()
    top_n = top_n or settings.rerank_top_n
    chat = chat or OllamaChat()
    # T11.4.1: the ONE process-wide shared instance (same as the live API) — one load for
    # the whole golden-set run, not one per item.
    reranker = get_shared_reranker()

    results: list[ItemResult] = []
    for item in items:
        chunks = retrieve(
            session, item.question, reranker=reranker, top_n=top_n, version=item.version
        )
        retrieved_docs = [_doc_slug(c.chunk_uid) for c in chunks]
        answer = answer_from_chunks(chat, item.question, chunks)

        correct: bool | None
        if not item.answerable:
            correct = None
        elif answer.abstained:
            correct = False
        else:
            correct = judge_correctness(chat, item.question, item.ground_truth, answer.text)

        results.append(
            ItemResult(
                id=item.id,
                answerable=item.answerable,
                retrieved_docs=retrieved_docs,
                expected_doc=item.expected_doc,
                abstained=answer.abstained,
                grounded=answer.grounded,
                correct=correct,
            )
        )
    return results


def evaluate_dialogues(
    session: Session,
    dialogues: list[Dialogue],
    *,
    chat: ChatClient | None = None,
    top_n: int | None = None,
) -> list[DialogueTurnResult]:
    """Replay golden-set v2 dialogues through TODAY's pipeline (T14.0.4, block 14-A2).

    **Placeholder core.** There is no shared conversational core and no LLM router/memory yet
    (T14.2.1/T14.2.2, block 14-B2): each turn is replayed STANDALONE, through the SAME decision
    point the live stream handler (``api/conversations.py`` ->
    ``generation.answer_question_stream``) uses today — ``classify_intent`` (chit-chat canned
    reply) or retrieve+answer — never a richer behaviour this block did not build. A dialogue's
    earlier turns are NOT passed as context
    (F14.2: the live stream handler only ever forwards ``request.question`` too). This under-builds
    on purpose: it is the "before" measurement T14.0.5's floors are compared against, and several
    floors are expected to be missed here precisely because the feature they measure (router,
    memory) does not exist until later blocks (14-0 re-check scope note; see the ADR).

    Uses ``make_chat_client()`` (TF21), unlike the v1 ``evaluate()`` above, which keeps its
    pre-existing hard-coded ``OllamaChat`` default unchanged (out of scope here — the v1 gate
    must stay green and untouched).

    ``is_noisy`` (noisy-query recall) is derived from the dialogue id's ``"typo-"`` prefix
    convention already used by ``eval/golden_set_v2.jsonl`` (every noisy item today is
    single-turn) rather than a new schema field — self-reported limitation: this convention
    would need a real per-turn flag if a future noisy item is added inside a multi-turn dialogue
    at a turn other than the first.
    """
    from rag_app.eval.judge import judge_correctness, judge_no_claims
    from rag_app.generation import _CHITCHAT_REPLY, answer_from_chunks, classify_intent
    from rag_app.reranking import get_shared_reranker, retrieve

    settings = get_settings()
    top_n = top_n or settings.rerank_top_n
    chat = chat or make_chat_client()
    reranker = get_shared_reranker()

    results: list[DialogueTurnResult] = []
    for dialogue in dialogues:
        is_noisy = dialogue.id.startswith("typo-")
        for index, turn in enumerate(dialogue.turns):
            start = time.perf_counter()
            observed_intent = (
                "chitchat" if classify_intent(turn.message) == "chitchat" else "security"
            )

            if observed_intent == "chitchat":
                results.append(
                    DialogueTurnResult(
                        dialogue_id=dialogue.id,
                        turn_index=index,
                        expected_intent=turn.expected_intent,
                        observed_intent="chitchat",
                        is_noisy=is_noisy,
                        retrieved_docs=[],
                        expected_doc=turn.expected_doc,
                        answerable=turn.answerable,
                        abstained=False,
                        reply_text=_CHITCHAT_REPLY,
                        correct=None,
                        clarified=False,
                        abstention_no_claims=None,
                        latency_s=time.perf_counter() - start,
                    )
                )
                continue

            chunks = retrieve(session, turn.message, reranker=reranker, top_n=top_n)
            retrieved_docs = [_doc_slug(c.chunk_uid) for c in chunks]
            answer = answer_from_chunks(chat, turn.message, chunks)

            correct: bool | None = None
            if turn.answerable:
                correct = (
                    False
                    if answer.abstained
                    else judge_correctness(chat, turn.message, turn.ground_truth or "", answer.text)
                )

            abstention_no_claims: bool | None = None
            if answer.abstained:
                abstention_no_claims = judge_no_claims(chat, turn.message, answer.text)

            results.append(
                DialogueTurnResult(
                    dialogue_id=dialogue.id,
                    turn_index=index,
                    expected_intent=turn.expected_intent,
                    observed_intent="security",
                    is_noisy=is_noisy,
                    retrieved_docs=retrieved_docs,
                    expected_doc=turn.expected_doc,
                    answerable=turn.answerable,
                    abstained=answer.abstained,
                    reply_text=answer.text,
                    correct=correct,
                    clarified=looks_like_clarifying_question(answer.text, bool(answer.citations)),
                    abstention_no_claims=abstention_no_claims,
                    latency_s=time.perf_counter() - start,
                )
            )
    return results
