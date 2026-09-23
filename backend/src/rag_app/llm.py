"""Chat client for the local LLM (qwen via Ollama).

Kept separate from embeddings.py; the HTTP call is isolated from message building
so callers/tests can construct prompts without a live server.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from rag_app.config import get_settings

_TIMEOUT_SECONDS = 180

Message = dict[str, str]


@dataclass
class Usage:
    """Token accounting for one or more LLM calls (from Ollama's *_count fields)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens


class OllamaChat:
    """Minimal client for the Ollama chat endpoint."""

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.llm_model
        self.keep_alive = settings.ollama_keep_alive

    def _options(self, temperature: float, max_tokens: int | None) -> dict[str, float | int]:
        options: dict[str, float | int] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        return options

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Send a chat request and return the assistant message content."""
        response = httpx.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": self._options(temperature, max_tokens),
            },
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        if not isinstance(content, str):
            raise ValueError(f"unexpected chat response: {content!r}")
        return content

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        usage: Usage | None = None,
    ) -> Iterator[str]:
        """Stream the assistant response token-by-token.

        Yields content deltas as they arrive. If a ``usage`` accumulator is passed,
        the final token counts (``prompt_eval_count`` / ``eval_count``) are added to
        it once the stream completes.
        """
        with httpx.stream(
            "POST",
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": True,
                "keep_alive": self.keep_alive,
                "options": self._options(temperature, max_tokens),
            },
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if chunk.get("done") and usage is not None:
                    usage.add(
                        Usage(
                            prompt_tokens=int(chunk.get("prompt_eval_count", 0)),
                            completion_tokens=int(chunk.get("eval_count", 0)),
                        )
                    )
