"""Markdown normalization and heading-aware chunking.

Chunks are split on semantic boundaries (Markdown headings), then long sections
are packed into overlapping windows so a retrieved chunk keeps surrounding context.
All functions here are pure and deterministic (easy to unit-test and to reason about
in the evaluation gate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_ATTR_RE = re.compile(r"\{:[^}]*\}")
_WHITESPACE_RE = re.compile(r"\s+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n{2,}")


@dataclass(frozen=True)
class Section:
    """A body of text under a heading breadcrumb (e.g. ``A1 > Injection``)."""

    heading: str
    body: str


@dataclass(frozen=True)
class Chunk:
    """A retrievable unit of text with its heading breadcrumb and position."""

    heading: str
    ordinal: int
    text: str

    @property
    def char_len(self) -> int:
        return len(self.text)


def clean_heading(raw: str) -> str:
    """Strip Markdown bold markers, inline images, attribute blocks and extra space."""
    text = _BOLD_RE.sub(r"\1", raw)
    text = _IMAGE_RE.sub("", text)
    text = _ATTR_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().strip("*").strip()


def normalize_markdown(md: str) -> str:
    """Clean up converter noise: drop U+FFFD, trim trailing spaces, collapse blanks."""
    md = md.replace("�", "")
    lines = [line.rstrip() for line in md.splitlines()]
    text = "\n".join(lines)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip() + "\n"


def split_into_sections(md: str) -> list[Section]:
    """Split Markdown into sections keyed by a heading breadcrumb."""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def breadcrumb() -> str:
        return " > ".join(text for _, text in stack)

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(Section(breadcrumb(), body))

    for line in md.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            buffer = []
            level = len(match.group(1))
            heading = clean_heading(match.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            if heading:
                stack.append((level, heading))
        else:
            buffer.append(line)
    flush()
    return sections


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """Character-window split for a single oversized paragraph."""
    step = max(1, size - overlap)
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end == len(text):
            break
        start += step
    return pieces


def _split_body(text: str, size: int, overlap: int) -> list[str]:
    """Pack paragraphs into chunks up to ``size`` with a trailing-char overlap."""
    if len(text) <= size:
        return [text]

    units: list[str] = []
    for para in _PARAGRAPH_SPLIT_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            units.append(para)
        else:
            units.extend(_hard_split(para, size, overlap))

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = unit if not current else f"{current}\n\n{unit}"
        if len(candidate) > size and current:
            chunks.append(current)
            tail = current[-overlap:].strip() if overlap > 0 else ""
            current = f"{tail}\n\n{unit}".strip() if tail else unit
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def chunk_markdown(md: str, *, chunk_size: int, overlap: int) -> list[Chunk]:
    """Normalize, split by heading, and pack into overlapping chunks."""
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    normalized = normalize_markdown(md)
    chunks: list[Chunk] = []
    ordinal = 0
    for section in split_into_sections(normalized):
        for piece in _split_body(section.body, chunk_size, overlap):
            chunks.append(Chunk(heading=section.heading, ordinal=ordinal, text=piece))
            ordinal += 1
    return chunks
