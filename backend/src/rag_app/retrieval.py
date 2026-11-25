"""Hybrid retrieval: dense vectors (pgvector) + keyword BM25 (tsvector), fused.

Vector search and BM25 each return a ranked list of chunk ids; the two lists are
merged with Reciprocal Rank Fusion (RRF), which needs no score calibration between
the very different scales of cosine distance and ts_rank.

**T14.2.9 (block 14-C3, DEC-062):** two fixes to the chunk-dominance failure DEC-062
diagnosed (status/blocks/14-C2.md + DA-14-C2.md, re-derived live against the dev corpus
with ``docker exec ... psql`` -- see the ADR's "14-C3 decision" section for the full
evidence):

1. ``_bm25_uids`` used ``plainto_tsquery`` (AND semantics): a 6+-word natural-language
   question ("What rank does injection have in the 2025 OWASP Top 10?") needs every one of
   its stemmed terms present in a chunk to match AT ALL -- confirmed live: this returned
   ZERO rows for that exact query, so BM25 contributed nothing to RRF and retrieval was
   silently vector-only. Rewritten to OR semantics (``_or_tsquery``, same trick as
   Postgres's own ``tsquery::text`` ``&``->``|`` replace) so a chunk matching SOME of the
   query's terms still ranks, restoring BM25 as a real second signal.
2. Even with OR semantics, a broad, generic multi-topic document (a slide-deck overview
   PDF, a long front-matter/release-notes PDF) can still occupy most or all of a small
   raw per-list SQL limit with many short, generically-worded chunks -- confirmed live:
   for one of DEC-062's 3 failing turns, 10/10 raw candidates from BOTH the vector and
   BM25 lists (even version-scoped) were the SAME one document, crowding out the only
   chunk from the actually-relevant document before fusion ever had a chance.
   :func:`hybrid_search` now fetches a WIDER raw pool per list (``_RAW_OVERSAMPLE``) and
   caps how many of the final ``top_k`` fused candidates one document may fill
   (:func:`diversify_candidates`) -- diversity happens only here, building the candidate
   POOL the cross-encoder sees; the cross-encoder itself still freely picks whichever
   ``top_n`` it judges best from that (now more diverse) pool, so a genuinely
   single-document-relevant question is not penalised at the final answer-context step.
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


def doc_slug(chunk_uid: str) -> str:
    """The document a chunk belongs to: ``chunk_uid`` is always ``"<doc_slug>::<ordinal>"``
    (``indexing.index_chunks``). Same convention ``eval.runner._doc_slug`` already uses --
    exported here too (T14.2.9) since :func:`diversify_candidates` needs it and this is the
    module that owns the chunk-id shape."""
    return chunk_uid.rsplit("::", 1)[0]


# T14.2.9 (DEC-062): how many of the raw-SQL per-list candidates to fetch per real slot the
# fused pool keeps -- wide enough that a document whose own chunks saturate the FIRST
# ``candidate_k`` positions of a raw list still leaves other documents' chunks reachable
# further down that same list, confirmed against the live corpus (one failing turn needed
# the 11th vector-search rank to reach the relevant document at all).
_RAW_OVERSAMPLE = 3

# How many of the final fused ``top_k`` candidates one document may occupy (T14.2.9): low
# enough that a document with many near-duplicate/boilerplate chunks (a slide deck, a PDF's
# front matter) cannot alone fill the whole candidate pool the cross-encoder sees; high
# enough that a question genuinely answered from 2-3 chunks of ONE relevant document is
# never starved of them (``settings.top_k`` is 10 by default; 9 corpus documents today).
_MAX_CHUNKS_PER_DOCUMENT_IN_CANDIDATES = 3


def diversify_candidates(
    fused: list[tuple[str, float]], *, top_k: int, max_per_document: int
) -> list[tuple[str, float]]:
    """Cap how many of the top ``top_k`` fused candidates can come from the same document.

    Keeps the SAME score order; a document that has already filled its share is skipped
    (not dropped) while scanning for more candidates. Backfills from the skipped overflow,
    in its own original order, if capping would otherwise return FEWER than ``top_k``
    entries -- diversity must never shrink the candidate pool below what fusion actually
    found (e.g. a single-document corpus, or every other document already exhausted).
    """
    counts: dict[str, int] = {}
    kept: list[tuple[str, float]] = []
    overflow: list[tuple[str, float]] = []
    for uid, score in fused:
        doc = doc_slug(uid)
        if counts.get(doc, 0) < max_per_document:
            counts[doc] = counts.get(doc, 0) + 1
            kept.append((uid, score))
        else:
            overflow.append((uid, score))
        if len(kept) == top_k:
            break
    if len(kept) < top_k:
        kept.extend(overflow[: top_k - len(kept)])
    return kept[:top_k]


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
    """Keyword search (T14.2.9's OR rewrite, see this module's own docstring): builds
    ``plainto_tsquery`` for its dictionary-aware tokenizing/stemming/stopword handling, then
    turns its implicit AND into OR (``' & '`` -> ``' | '`` on the tsquery's own text form, the
    same trick Postgres recipes use) so a chunk matching SOME query terms still ranks instead
    of a multi-word natural-language question matching nothing at all."""
    version_filter = "AND version = :version" if version else ""
    sql = text(
        "SELECT chunk_uid FROM chunks, "
        "(SELECT replace(plainto_tsquery('english', :q)::text, ' & ', ' | ')::tsquery AS q) "
        "AS parsed "  # noqa: S608 (bound params)
        "WHERE tsv @@ parsed.q "
        f"{version_filter} "
        "ORDER BY ts_rank_cd(tsv, parsed.q) DESC LIMIT :k"
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

    # T14.2.9 (DEC-062): fetch a WIDER raw pool per list than the final fused ``top_k`` --
    # see this module's own docstring. Only widens the raw SQL fetch; `top_k`'s own meaning
    # (the size of what `hybrid_search` returns) is unchanged for every existing caller.
    raw_candidate_k = max(candidate_k, top_k * _RAW_OVERSAMPLE)

    with timing.stage("hybrid_search"):
        with timing.step("retrieving", "vector_search"):
            vector_uids = _vector_uids(session, query_vector, raw_candidate_k, version)
        with timing.step("retrieving", "bm25_search"):
            bm25_uids = _bm25_uids(session, query, raw_candidate_k, version)

        with timing.step("retrieving", "rrf_fusion"):
            fused_all = reciprocal_rank_fusion([vector_uids, bm25_uids])
            fused = diversify_candidates(
                fused_all,
                top_k=top_k,
                max_per_document=_MAX_CHUNKS_PER_DOCUMENT_IN_CANDIDATES,
            )
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
