"""Source document -> Markdown conversion.

PDFs are converted with ``pymupdf4llm`` (layout-aware, keeps headings/tables).
Source Markdown files are read as-is; normalization happens in the chunking step.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf4llm


def pdf_to_markdown(path: Path) -> str:
    """Convert a PDF to Markdown using pymupdf4llm."""
    result: str = pymupdf4llm.to_markdown(str(path), show_progress=False)
    return result


def read_markdown(path: Path) -> str:
    """Read a source Markdown file."""
    return path.read_text(encoding="utf-8")
