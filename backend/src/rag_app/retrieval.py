"""Hybrid retrieval: dense vectors (pgvector) + keyword BM25 (tsvector), fused.

Vector search and BM25 each return a ranked list of chunk ids; the two lists are
merged with Reciprocal Rank Fusion (RRF), which needs no score calibration between
the very different scales of cosine distance and ts_rank.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from rag_app import timing
from rag_app.config import get_settings
from rag_app.db.session import make_session_factory
from rag_app.embeddings import OllamaEmbedder


@dataclass
class RetrievedChunk:
    chunk_uid: str
    heading: str
    text: str
    version: str
    effective_date: str | None
    score: float


def reciprocal_rank_fusion(
    result_lists: list[list[str]], *, k0: int = 60
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one ranking. ``k0`` damps the weight of low ranks."""
    scores: dict[str, float] = {}
    for results in result_lists:
        for rank, uid in enumerate(results):
            scores[uid] = scores.get(uid, 0.0) + 1.0 / (k0 + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _to_vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _vector_uids(
    session: Session, query_vector: list[float], limit: int, version: str | None
) -> list[str]:
    where = "WHERE version = :version" if version else ""
    sql = text(
        f"SELECT chunk_uid FROM chunks {where} "  # noqa: S608 (version is a bound param)
        "ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :k"
    )
    params = {"qvec": _to_vector_literal(query_vector), "k": limit}
    if version:
        params["version"] = version
    return [row[0] for row in session.execute(sql, params)]


def _bm25_uids(session: Session, query: str, limit: int, version: str | None) -> list[str]:
    version_filter = "AND version = :version" if version else ""
    sql = text(
        "SELECT chunk_uid FROM chunks "
        "WHERE tsv @@ plainto_tsquery('english', :q) "  # noqa: S608 (bound params)
        f"{version_filter} "
        "ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', :q)) DESC LIMIT :k"
    )
    params = {"q": query, "k": limit}
    if version:
        params["version"] = version
    return [row[0] for row in session.execute(sql, params)]


def _load_chunks(session: Session, uids: list[str]) -> dict[str, RetrievedChunk]:
    if not uids:
        return {}
    sql = text(
        "SELECT chunk_uid, heading, text, version, effective_date "
        "FROM chunks WHERE chunk_uid IN :uids"
    ).bindparams(bindparam("uids", expanding=True))
    result: dict[str, RetrievedChunk] = {}
    for row in session.execute(sql, {"uids": uids}):
        effective = row.effective_date.isoformat() if row.effective_date else None
        result[row.chunk_uid] = RetrievedChunk(
            chunk_uid=row.chunk_uid,
            heading=row.heading,
            text=row.text,
            version=row.version,
            effective_date=effective,
            score=0.0,
        )
    return result


def embed_query(embedder: OllamaEmbedder, query: str) -> list[float]:
    """Embed one query string -- the ``retrieving``/``embed_query`` atomic step (T13.8.1).

    T13.12.5: pulled out of :func:`hybrid_search` so the streaming pipeline
    (``generation.answer_question_stream``) can call it directly and bracket it with its
    own SSE sub-step events (``StreamStep``) -- this is the dominant cold-start cost (Ollama
    waking ``bge-m3``), and the ONLY way to surface a real "still working" signal to the UI
    while it runs is for the caller's own generator frame to ``yield`` around it; a callback
    passed into ``hybrid_search`` could not do that (a plain function cannot ``yield`` on a
    generator's behalf). Every non-streaming caller still gets this for free via
    ``hybrid_search``, unchanged.
    """
    with timing.stage("embed"), timing.step("retrieving", "embed_query"):
        return embedder.embed_one(query)


def hybrid_search(
    session: Session,
    query: str,
    *,
    embedder: OllamaEmbedder | None = None,
    top_k: int | None = None,
    candidate_k: int | None = None,
    version: str | None = None,
    query_vector: list[float] | None = None,
) -> list[RetrievedChunk]:
    """Return the top chunks for a query, fusing vector and BM25 rankings.

    ``query_vector`` (T13.12.5): when the caller already computed it (see
    :func:`embed_query`'s docstring), it is reused as-is and ``embedder`` is never touched
    -- every other caller is unaffected, the embed call still happens right here, exactly
    as before.
    """
    settings = get_settings()
    top_k = top_k or settings.rerank_top_n
    candidate_k = candidate_k or settings.top_k

    # T13.8.1: the atomic-step wrap below is a SEPARATE channel (``secrag_chat_step_seconds``,
    # pipeline stage "retrieving") -- the outer ``timing.stage(...)`` calls and their meaning
    # (the existing ``secrag_chat_stage_seconds`` histogram) are unchanged.
    if query_vector is None:
        embedder = embedder or OllamaEmbedder()
        query_vector = embed_query(embedder, query)

    with timing.stage("hybrid_search"):
        with timing.step("retrieving", "vector_search"):
            vector_uids = _vector_uids(session, query_vector, candidate_k, version)
        with timing.step("retrieving", "bm25_search"):
            bm25_uids = _bm25_uids(session, query, candidate_k, version)

        with timing.step("retrieving", "rrf_fusion"):
            fused = reciprocal_rank_fusion([vector_uids, bm25_uids])[:top_k]
        # DA-13-G1-1(a): the ranked-list build below is attach-score-and-collect bookkeeping
        # over the SAME chunks `_load_chunks` just materialized -- the nearest truthful
        # boundary is to keep it inside the "load_chunks" step rather than leaving it
        # untracked (still inside `stage("hybrid_search")`, so the stage total itself is
        # unaffected; only the step/stage split changes).
        with timing.step("retrieving", "load_chunks"):
            chunks = _load_chunks(session, [uid for uid, _ in fused])
            ranked: list[RetrievedChunk] = []
            for uid, score in fused:
                chunk = chunks.get(uid)
                if chunk is not None:
                    chunk.score = score
                    ranked.append(chunk)
    return ranked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the corpus (hybrid retrieval).")
    parser.add_argument("query", help="the question to search for")
    parser.add_argument("--version", default=None, help="filter by corpus version")
    parser.add_argument("--top-k", type=int, default=None)
    args = parser.parse_args(argv)

    session_factory = make_session_factory()
    with session_factory() as session:
        results = hybrid_search(session, args.query, top_k=args.top_k, version=args.version)

    print(f'\nQuery: {args.query!r}  (version={args.version or "any"})\n')
    for i, chunk in enumerate(results, start=1):
        snippet = " ".join(chunk.text.split())[:160]
        print(f"{i}. [{chunk.version}] {chunk.heading[:60]}  (score={chunk.score:.4f})")
        print(f"   {snippet}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
