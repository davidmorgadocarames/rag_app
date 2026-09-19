"""Embeddings via Ollama (bge-m3).

The HTTP call is isolated from response parsing so the parsing logic can be
unit-tested without a live Ollama server.
"""

from __future__ import annotations

from typing import Any

import httpx

from rag_app.config import get_settings

_TIMEOUT_SECONDS = 120


def parse_embed_response(data: dict[str, Any]) -> list[list[float]]:
    """Extract the list of embedding vectors from an Ollama /api/embed response."""
    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list) or not embeddings:
        raise ValueError(f"unexpected embeddings response: {data!r}")
    return [[float(x) for x in vector] for vector in embeddings]


class OllamaEmbedder:
    """Minimal client for the Ollama embeddings endpoint."""

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.embed_model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        if not texts:
            return []
        response = httpx.post(
            f"{self.host}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return parse_embed_response(response.json())

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text and return its vector."""
        return self.embed([text])[0]
