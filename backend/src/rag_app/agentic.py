"""Agentic RAG router (CRAG-lite).

If the first retrieval looks "thin" (best cross-encoder score below a threshold),
reformulate the query into precise security terminology and retry, keeping the
better result set. Then answer normally (which still abstains if unsupported).

Whether this beats the simple pipeline is decided by rag_app.eval.benchmark — the
router is only worth keeping if the numbers justify the added latency/cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from rag_app.config import get_settings
from rag_app.generation import Answer, answer_from_chunks
from rag_app.llm import Message, OllamaChat
from rag_app.retrieval import RetrievedChunk

if TYPE_CHECKING:
    from rag_app.reranking import CrossEncoderReranker

_REWRITE_SYSTEM = (
    "Rewrite the user's question using precise OWASP / application-security "
    "terminology to improve document retrieval. Keep it a single question and output "
    "only the rewritten question, with no preamble."
)


@dataclass
class RoutedAnswer:
    answer: Answer
    retrieved_docs: list[str]
    rewrote: bool


def _doc_slug(chunk_uid: str) -> str:
    return chunk_uid.rsplit("::", 1)[0]


def _best_score(chunks: list[RetrievedChunk]) -> float:
    return max((c.score for c in chunks), default=0.0)


def is_thin(chunks: list[RetrievedChunk], threshold: float) -> bool:
    """True when retrieval is empty or its best chunk scores below the threshold."""
    return _best_score(chunks) < threshold


def reformulate(chat: OllamaChat, query: str) -> str:
    messages: list[Message] = [
        {"role": "system", "content": _REWRITE_SYSTEM},
        {"role": "user", "content": query},
    ]
    return chat.chat(messages, temperature=0.0).strip()


def answer_agentic(
    session: Session,
    query: str,
    *,
    chat: OllamaChat | None = None,
    reranker: CrossEncoderReranker | None = None,
    top_n: int | None = None,
    version: str | None = None,
) -> RoutedAnswer:
    """Retrieve; if thin, reformulate + retry; then generate a grounded answer."""
    from rag_app.reranking import CrossEncoderReranker, retrieve

    settings = get_settings()
    top_n = top_n or settings.rerank_top_n
    chat = chat or OllamaChat()
    reranker = reranker or CrossEncoderReranker()

    chunks = retrieve(session, query, reranker=reranker, top_n=top_n, version=version)
    rewrote = False
    rewrites = 0
    while is_thin(chunks, settings.thin_threshold) and rewrites < settings.max_query_rewrites:
        rewrites += 1
        new_query = reformulate(chat, query)
        new_chunks = retrieve(session, new_query, reranker=reranker, top_n=top_n, version=version)
        if _best_score(new_chunks) > _best_score(chunks):
            chunks = new_chunks
            rewrote = True

    answer = answer_from_chunks(chat, query, chunks)
    return RoutedAnswer(
        answer=answer, retrieved_docs=[_doc_slug(c.chunk_uid) for c in chunks], rewrote=rewrote
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    from rag_app.db.session import make_session_factory

    parser = argparse.ArgumentParser(description="Ask via the agentic router.")
    parser.add_argument("query")
    parser.add_argument("--version", default=None)
    args = parser.parse_args(argv)

    with make_session_factory()() as session:
        routed = answer_agentic(session, args.query, version=args.version)

    print(f"\nQ: {args.query}  (rewrote={routed.rewrote})\n")
    print(routed.answer.text)
    if routed.answer.abstained:
        print("\n[abstained]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
