"""Corpus ingestion: PDF/Markdown -> normalized Markdown -> chunks."""

from rag_app.ingestion.chunking import Chunk, Section, chunk_markdown, normalize_markdown
from rag_app.ingestion.pipeline import IngestStats, run_ingestion

__all__ = [
    "Chunk",
    "IngestStats",
    "Section",
    "chunk_markdown",
    "normalize_markdown",
    "run_ingestion",
]
