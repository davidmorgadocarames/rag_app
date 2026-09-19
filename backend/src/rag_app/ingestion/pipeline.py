"""Ingestion pipeline: manifest -> normalized Markdown + chunks.jsonl.

Reads ``data/corpus_manifest.json`` (produced by ``scripts/fetch_corpus.py``),
converts each source to Markdown, writes the normalized Markdown to
``data/markdown/`` (human-inspectable), chunks it, and writes every chunk with its
metadata to ``data/chunks/chunks.jsonl``.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag_app.ingestion.chunking import chunk_markdown, normalize_markdown
from rag_app.ingestion.convert import pdf_to_markdown, read_markdown

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_DIR = REPO_ROOT / "data"


@dataclass
class IngestStats:
    """Summary of an ingestion run."""

    documents: int = 0
    chunks: int = 0
    skipped: int = 0
    chars: int = 0
    chunks_by_version: Counter[str] = field(default_factory=Counter)

    @property
    def avg_chunk_chars(self) -> int:
        return self.chars // self.chunks if self.chunks else 0


def _source_path(data_dir: Path, item: dict[str, Any]) -> Path:
    sub = "raw_pdfs" if item["kind"] == "pdf" else "raw_md"
    return data_dir / sub / str(item["filename"])


def run_ingestion(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> IngestStats:
    """Run the full ingestion over the corpus manifest."""
    manifest_path = data_dir / "corpus_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"{manifest_path} not found. Run scripts/fetch_corpus.py first.")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    markdown_dir = data_dir / "markdown"
    chunks_dir = data_dir / "chunks"
    markdown_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    stats = IngestStats()
    records: list[dict[str, Any]] = []

    for item in manifest.get("items", []):
        source_path = _source_path(data_dir, item)
        if item.get("status") == "failed" or not source_path.exists():
            stats.skipped += 1
            continue

        if item["kind"] == "pdf":
            raw_markdown = pdf_to_markdown(source_path)
        else:
            raw_markdown = read_markdown(source_path)

        markdown = normalize_markdown(raw_markdown)
        slug = item["slug"]
        (markdown_dir / f"{slug}.md").write_text(markdown, encoding="utf-8")

        chunks = chunk_markdown(markdown, chunk_size=chunk_size, overlap=chunk_overlap)
        for chunk in chunks:
            records.append(
                {
                    "id": f"{slug}::{chunk.ordinal}",
                    "doc_slug": slug,
                    "source": item.get("url"),
                    "title": item.get("title"),
                    "version": item.get("version"),
                    "effective_date": item.get("effective_date"),
                    "category_rank": item.get("category_rank"),
                    "heading": chunk.heading,
                    "ordinal": chunk.ordinal,
                    "text": chunk.text,
                    "char_len": chunk.char_len,
                }
            )
            stats.chars += chunk.char_len
            stats.chunks_by_version[str(item.get("version"))] += 1

        stats.documents += 1
        stats.chunks += len(chunks)

    chunks_path = chunks_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return stats
