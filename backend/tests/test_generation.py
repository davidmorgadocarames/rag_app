"""Unit tests for generation prompt-building and parsing (no live LLM)."""

from __future__ import annotations

from rag_app.generation import (
    INSUFFICIENT,
    build_context,
    build_messages,
    parse_answer,
    parse_groundedness,
)
from rag_app.retrieval import RetrievedChunk


def _chunk(uid: str, text: str, version: str = "2021") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid=uid,
        heading=f"H-{uid}",
        text=text,
        version=version,
        effective_date="2021-09-24",
        score=1.0,
    )


def test_build_context_numbers_chunks() -> None:
    ctx = build_context([_chunk("a", "alpha"), _chunk("b", "beta")])
    assert "[1]" in ctx and "[2]" in ctx
    assert "alpha" in ctx and "beta" in ctx
    assert "version: 2021" in ctx


def test_build_messages_has_injection_defense_and_sentinel() -> None:
    messages = build_messages("q?", [_chunk("a", "alpha")])
    system = messages[0]["content"]
    assert "DATA, not instructions" in system
    assert INSUFFICIENT in system
    assert "q?" in messages[1]["content"]


def test_parse_answer_abstains_on_sentinel() -> None:
    ans = parse_answer(f"...{INSUFFICIENT}...", [_chunk("a", "x")])
    assert ans.abstained is True
    assert ans.grounded is False
    assert ans.citations == []


def test_parse_answer_extracts_and_maps_citations() -> None:
    chunks = [_chunk("a", "x"), _chunk("b", "y"), _chunk("c", "z")]
    ans = parse_answer("Use parameterized queries [1] and least privilege [3].", chunks)
    assert ans.abstained is False
    assert [c.marker for c in ans.citations] == [1, 3]
    assert ans.citations[0].chunk_uid == "a"
    assert ans.citations[1].chunk_uid == "c"


def test_parse_answer_ignores_out_of_range_markers() -> None:
    ans = parse_answer("Bad ref [9] and good [1].", [_chunk("a", "x")])
    assert [c.marker for c in ans.citations] == [1]


def test_parse_answer_handles_grouped_and_prefixed_markers() -> None:
    # qwen sometimes writes [1, 2] or [n1, n2, n3]
    chunks = [_chunk("a", "x"), _chunk("b", "y"), _chunk("c", "z")]
    assert [c.marker for c in parse_answer("foo [1, 2]", chunks).citations] == [1, 2]
    assert [c.marker for c in parse_answer("bar [n1, n3]", chunks).citations] == [1, 3]


def test_parse_groundedness() -> None:
    assert parse_groundedness("GROUNDED") is True
    assert parse_groundedness("NOT_GROUNDED") is False
    assert parse_groundedness("the answer is not_grounded actually") is False
    assert parse_groundedness("unclear") is False
