"""Unit tests for embedding response parsing (no live Ollama required)."""

from __future__ import annotations

import pytest

from rag_app.embeddings import parse_embed_response


def test_parse_embed_response_returns_float_vectors() -> None:
    data = {"embeddings": [[1, 2, 3], [4, 5, 6]]}
    vectors = parse_embed_response(data)
    assert vectors == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert all(isinstance(x, float) for x in vectors[0])


def test_parse_embed_response_rejects_missing_key() -> None:
    with pytest.raises(ValueError, match="embeddings"):
        parse_embed_response({"error": "no model"})


def test_parse_embed_response_rejects_empty() -> None:
    with pytest.raises(ValueError, match="embeddings"):
        parse_embed_response({"embeddings": []})
