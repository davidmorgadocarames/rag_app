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
    """Replay golden-set v2 dialogues through the REAL shared understanding step
    (T14.2.1-T14.2.3, block 14-B2): each turn calls ``rag_app.assistant.core.understand`` --
    the SAME function ``generation.answer_question``/``answer_question_stream`` now call too
    (fast path, then the LLM router) -- closing TF19's "eval runner bypasses the real
    pipeline" finding for the understanding step. The retrieve/generate/groundedness-check
    part still calls the same shared primitives (``reranking.retrieve``,
    ``generation.answer_from_chunks``) this runner already used before 14-B2 (and
    ``evaluate()`` above still does) -- not duplicated into a new module, see
    ``assistant/core.py``'s own scope note on why that part was not relocated.

    T14.2.5/T14.2.6 (block 14-C1): memory (``summary``/``last_turn``) is now wired in, the
    THIRD entry point named by DEC-059's option A -- carried IN-MEMORY across one dialogue's
    own turns (reset for every new ``dialogue``, never across dialogues: each golden-set
    dialogue is its own, independent conversation; there is no DB/encryption here, a
    dialogue is not a real account). ``rag_app.assistant.memory.summarize_turn`` updates it
    after every turn exactly like the real stream path does, right down to the same
    "dropped, not crashed" behaviour on an invalid/unreachable update (``summary_text`` then
    simply stays whatever it already was) -- EXCEPT a fast-path chit-chat turn
    (``decision.used_fast_path``), which skips the write entirely (DA-14-C1 finding 1 fix,
    14-C1-fix): the identical "no second, uncounted LLM call on a cap-exempt turn" rule
    ``api.conversations._update_conversation_summary`` now follows. Retrieval below still
    uses the turn's own raw ``message``, never the router's ``standalone_query``
    (T14.2.7/14-C2 — not this block), so
    ``follow_up_resolution`` is expected to stay unchanged by this wiring.
    ``observed_intent`` is the real 5-way ``RouterOutput.intent`` (or ``"chitchat"`` from the
    exact fast path, which never calls the router) instead of the old placeholder's
    chitchat/security binary; ``router_latency_s`` is the first real measurement for the
    T14.0.5 "router p95" floor.

    Uses ``make_chat_client()`` (TF21), unlike the v1 ``evaluate()`` above, which keeps its
    pre-existing hard-coded ``OllamaChat`` default unchanged (out of scope here — the v1 gate
    must stay green and untouched).

    ``is_noisy`` (noisy-query recall) is derived from the dialogue id's ``"typo-"`` prefix
    convention already used by ``eval/golden_set_v2.jsonl`` (every noisy item today is
    single-turn) rather than a new schema field — self-reported limitation: this convention
    would need a real per-turn flag if a future noisy item is added inside a multi-turn dialogue
    at a turn other than the first.
    """
    from rag_app.assistant.core import understand
    from rag_app.assistant.fast_path import generate_chitchat_reply
    from rag_app.assistant.memory import format_last_turn, summarize_turn
    from rag_app.eval.judge import judge_correctness, judge_no_claims
    from rag_app.generation import Answer, answer_from_chunks
    from rag_app.reranking import get_shared_reranker, retrieve

    settings = get_settings()
    top_n = top_n or settings.rerank_top_n
    chat = chat or make_chat_client()
    reranker = get_shared_reranker()

    results: list[DialogueTurnResult] = []
    for dialogue in dialogues:
        is_noisy = dialogue.id.startswith("typo-")
        # T14.2.5/T14.2.6: memory carried IN-MEMORY across this dialogue's own turns only --
        # reset for every new dialogue (see this function's own docstring).
        summary_text: str | None = None
        last_turn_text: str | None = None
        for index, turn in enumerate(dialogue.turns):
            start = time.perf_counter()
            decision = understand(
                chat, turn.message, summary=summary_text, last_turn=last_turn_text
            )

            if decision.intent == "chitchat":
                reply = generate_chitchat_reply(chat, turn.message, decision.language)
                answer: Answer = Answer(text=reply, abstained=False, grounded=True)
                retrieved_docs: list[str] = []
            else:
                chunks = retrieve(
                    session, turn.message, reranker=reranker, top_n=top_n, version=None
                )
                retrieved_docs = [_doc_slug(c.chunk_uid) for c in chunks]
                answer = answer_from_chunks(chat, turn.message, chunks)
            latency_s = time.perf_counter() - start

            # DA-14-C1 finding 1 fix (14-C1-fix, HIGH): the same gap as /chat/stream's
            # ``_update_conversation_summary`` -- a fast-path chit-chat turn
            # (``decision.used_fast_path``) has nothing worth summarizing and must not add a
            # second, uncounted LLM call on top of the chit-chat reply. ``last_turn_text`` is
            # still carried forward unconditionally: it reflects whatever the turn actually
            # was (fast path or not), same as the real stream's DB-backed memory read always
            # picks up the last stored turn regardless of how it was answered.
            if not decision.used_fast_path:
                new_summary = summarize_turn(
                    chat,
                    previous_summary=summary_text,
                    last_turn=last_turn_text,
                    question=turn.message,
                    answer=answer.text,
                )
                if new_summary is not None:
                    summary_text = new_summary
            last_turn_text = format_last_turn(turn.message, answer.text)

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
                    observed_intent=decision.intent,
                    is_noisy=is_noisy,
                    retrieved_docs=retrieved_docs,
                    expected_doc=turn.expected_doc,
                    answerable=turn.answerable,
                    abstained=answer.abstained,
                    reply_text=answer.text,
                    correct=correct,
                    clarified=looks_like_clarifying_question(answer.text, bool(answer.citations)),
                    abstention_no_claims=abstention_no_claims,
                    latency_s=latency_s,
                    router_latency_s=decision.router_latency_s,
                )
            )
    return results
