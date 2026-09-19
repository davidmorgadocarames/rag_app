"""Cross-encoder reranking (bge-reranker).

Retrieval (vector + BM25) is fast but approximate; a cross-encoder re-scores each
(query, chunk) pair jointly for higher precision, at the cost of latency. The model
is heavy (torch), so it is imported lazily: importing this module stays cheap and the
pure ordering logic is unit-testable without loading torch.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from rag_app.config import get_settings
from rag_app.db.session import make_session_factory
from rag_app.retrieval import RetrievedChunk, hybrid_search

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder


def order_by_scores(
    candidates: list[RetrievedChunk], scores: list[float], top_n: int
) -> list[RetrievedChunk]:
    """Attach rerank scores, sort by them (descending), and keep the top ``top_n``."""
    if len(candidates) != len(scores):
        raise ValueError("candidates and scores must have the same length")
    for candidate, score in zip(candidates, scores, strict=True):
        candidate.score = score
    ranked = sorted(candidates, key=lambda chunk: chunk.score, reverse=True)
    return ranked[:top_n]


class CrossEncoderReranker:
    """Reranks retrieved chunks with a bge-reranker cross-encoder."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or get_settings().reranker_model
        self._model: CrossEncoder | None = None

    def _ensure_model(self) -> CrossEncoder:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(
        self, query: str, candidates: list[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        """Re-score candidates against the query and return the best ``top_n``."""
        if not candidates:
            return []
        model = self._ensure_model()
        pairs = [[query, candidate.text] for candidate in candidates]
        scores = model.predict(pairs)
        return order_by_scores(candidates, [float(score) for score in scores], top_n)


def retrieve(
    session: Session,
    query: str,
    *,
    reranker: CrossEncoderReranker,
    candidate_k: int | None = None,
    top_n: int | None = None,
    version: str | None = None,
) -> list[RetrievedChunk]:
    """Hybrid retrieve a candidate pool, then rerank down to ``top_n``."""
    settings = get_settings()
    candidate_k = candidate_k or settings.top_k
    top_n = top_n or settings.rerank_top_n
    candidates = hybrid_search(
        session, query, top_k=candidate_k, candidate_k=candidate_k, version=version
    )
    return reranker.rerank(query, candidates, top_n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query with hybrid retrieval + rerank.")
    parser.add_argument("query", help="the question to search for")
    parser.add_argument("--version", default=None, help="filter by corpus version")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--candidate-k", type=int, default=None)
    parser.add_argument("--model", default=None, help="override the reranker model")
    args = parser.parse_args(argv)

    reranker = CrossEncoderReranker(model_name=args.model)
    session_factory = make_session_factory()
    with session_factory() as session:
        results = retrieve(
            session,
            args.query,
            reranker=reranker,
            candidate_k=args.candidate_k,
            top_n=args.top_n,
            version=args.version,
        )

    print(f'\nQuery: {args.query!r}  (reranked, version={args.version or "any"})\n')
    for i, chunk in enumerate(results, start=1):
        snippet = " ".join(chunk.text.split())[:160]
        print(f"{i}. [{chunk.version}] {chunk.heading[:60]}  (score={chunk.score:.4f})")
        print(f"   {snippet}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
