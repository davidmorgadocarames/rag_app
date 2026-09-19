"""CLI for the ingestion pipeline: ``python -m rag_app.ingestion``."""

from __future__ import annotations

import argparse
from pathlib import Path

from rag_app.config import get_settings
from rag_app.ingestion.pipeline import DEFAULT_DATA_DIR, run_ingestion


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Ingest the OWASP corpus into chunks.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--chunk-size", type=int, default=settings.chunk_size)
    parser.add_argument("--chunk-overlap", type=int, default=settings.chunk_overlap)
    args = parser.parse_args(argv)

    stats = run_ingestion(
        args.data_dir,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )

    print(f"\nIngestion complete -> {args.data_dir}\n")
    print(f"  documents:      {stats.documents}")
    print(f"  chunks:         {stats.chunks}")
    print(f"  skipped:        {stats.skipped}")
    print(f"  avg chunk size: {stats.avg_chunk_chars} chars")
    print("  chunks by version:")
    for version, count in sorted(stats.chunks_by_version.items()):
        print(f"    {version:10} {count}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
