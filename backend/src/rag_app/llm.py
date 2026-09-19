"""Chat client for the local LLM (qwen via Ollama).

Kept separate from embeddings.py; the HTTP call is isolated from message building
so callers/tests can construct prompts without a live server.
"""

from __future__ import annotations

import httpx

from rag_app.config import get_settings

_TIMEOUT_SECONDS = 180

Message = dict[str, str]


class OllamaChat:
    """Minimal client for the Ollama chat endpoint."""

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.llm_model

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Send a chat request and return the assistant message content."""
        options: dict[str, float | int] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        response = httpx.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": options,
            },
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        if not isinstance(content, str):
            raise ValueError(f"unexpected chat response: {content!r}")
        return content
