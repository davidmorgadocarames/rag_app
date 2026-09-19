"""Run the RAG pipeline over the golden set and collect per-item results."""

from __future__ import annotations

from sqlalchemy.orm import Session

from rag_app.config import get_settings
from rag_app.eval.dataset import GoldenItem
from rag_app.eval.metrics import ItemResult
from rag_app.llm import OllamaChat


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
    from rag_app.reranking import CrossEncoderReranker, retrieve

    settings = get_settings()
    top_n = top_n or settings.rerank_top_n
    chat = chat or OllamaChat()
    reranker = CrossEncoderReranker()

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
