"""Index chunks into PostgreSQL/pgvector.

Reads ``data/chunks/chunks.jsonl`` (produced by ``rag_app.ingestion``), embeds each
chunk with bge-m3 (via Ollama), and writes documents + chunks to the database.
The load is a full refresh (wipe + insert) so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from rag_app.db.models import Chunk, Document
from rag_app.db.session import make_session_factory
from rag_app.embeddings import OllamaEmbedder
from rag_app.ingestion.pipeline import DEFAULT_DATA_DIR

_EMBED_BATCH = 32


def _parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def load_chunk_records(path: Path) -> list[dict[str, Any]]:
    """Load chunk records from a JSONL file."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _embed_all(embedder: OllamaEmbedder, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        vectors.extend(embedder.embed(texts[start : start + _EMBED_BATCH]))
    return vectors


def index_chunks(
    session: Session,
    records: list[dict[str, Any]],
    embedder: OllamaEmbedder,
) -> int:
    """Refresh documents/chunks from records; returns the number of chunks written."""
    documents: dict[str, dict[str, Any]] = {}
    for record in records:
        documents.setdefault(record["doc_slug"], record)

    session.query(Chunk).delete()
    session.query(Document).delete()
    session.flush()

    doc_objects: dict[str, Document] = {}
    for slug, record in documents.items():
        document = Document(
            slug=slug,
            source=record.get("source"),
            title=record.get("title"),
            version=record["version"],
            effective_date=_parse_date(record.get("effective_date")),
            category_rank=record.get("category_rank"),
        )
        session.add(document)
        doc_objects[slug] = document
    session.flush()

    vectors = _embed_all(embedder, [record["text"] for record in records])
    for record, vector in zip(records, vectors, strict=True):
        session.add(
            Chunk(
                document_id=doc_objects[record["doc_slug"]].id,
                chunk_uid=record["id"],
                heading=record.get("heading", ""),
                ordinal=record["ordinal"],
                text=record["text"],
                embedding=vector,
                version=record["version"],
                effective_date=_parse_date(record.get("effective_date")),
            )
        )
    session.commit()
    return len(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index chunks into pgvector.")
    parser.add_argument(
        "--chunks",
        type=Path,
        default=DEFAULT_DATA_DIR / "chunks" / "chunks.jsonl",
    )
    args = parser.parse_args(argv)

    records = load_chunk_records(args.chunks)
    session_factory = make_session_factory()
    with session_factory() as session:
        count = index_chunks(session, records, OllamaEmbedder())

    print(f"\nIndexed {count} chunks from {args.chunks}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
