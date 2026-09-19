"""Unit tests for Markdown normalization and chunking."""

from __future__ import annotations

import pytest

from rag_app.ingestion.chunking import (
    chunk_markdown,
    clean_heading,
    normalize_markdown,
    split_into_sections,
)


def test_normalize_removes_replacement_chars_and_blank_runs() -> None:
    md = "# Title\n\n\n\nbody with �� noise \n"
    out = normalize_markdown(md)
    assert "�" not in out  # U+FFFD replacement chars are dropped
    assert "\n\n\n" not in out
    assert out.endswith("\n")


def test_clean_heading_strips_bold() -> None:
    assert clean_heading("**A1:2017 Injection**") == "A1:2017 Injection"


def test_sections_use_heading_breadcrumb() -> None:
    md = "# A1\n\n## Injection\n\nUse parameterized queries.\n"
    sections = split_into_sections(md)
    assert len(sections) == 1
    assert sections[0].heading == "A1 > Injection"
    assert "parameterized" in sections[0].body


def test_small_document_yields_single_chunk() -> None:
    md = "# Heading\n\nShort body.\n"
    chunks = chunk_markdown(md, chunk_size=1200, overlap=150)
    assert len(chunks) == 1
    assert chunks[0].heading == "Heading"
    assert chunks[0].ordinal == 0


def test_long_section_is_split_with_overlap() -> None:
    body = "\n\n".join(f"Paragraph number {i} with some filler text." for i in range(60))
    md = f"# Big\n\n{body}\n"
    chunks = chunk_markdown(md, chunk_size=300, overlap=60)
    assert len(chunks) > 1
    # every chunk respects the size budget (allowing for the overlap tail)
    assert all(c.char_len <= 300 + 60 for c in chunks)
    # ordinals are contiguous
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunk_markdown("# x\n\nbody\n", chunk_size=100, overlap=100)
