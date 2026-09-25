"""Chat clients for the LLM tier.

Two interchangeable providers implement the same ``ChatClient`` interface:
``OllamaChat`` (local qwen, the free/dev default) and ``AzureOpenAIChat`` (the hosted
cloud provider). ``make_chat_client`` picks one from ``LLM_PROVIDER``. The HTTP call is
isolated from message building so callers/tests can construct prompts without a live
server.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from rag_app.config import Settings, get_settings

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


class ChatClient(Protocol):
    """Structural interface shared by every LLM provider.

    Both ``OllamaChat`` and ``AzureOpenAIChat`` satisfy this; callers depend on the
    protocol, so swapping providers only changes ``make_chat_client``.
    """

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str: ...

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        usage: Usage | None = None,
    ) -> Iterator[str]: ...


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


def parse_azure_chat_response(data: dict[str, Any]) -> str:
    """Extract the assistant message content from an Azure OpenAI chat response."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"unexpected azure chat response: {data!r}")
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str):
        raise ValueError(f"unexpected azure chat response: {data!r}")
    return content


class AzureOpenAIChat:
    """Client for Azure OpenAI chat completions (the hosted cloud provider).

    Hand-rolled on httpx to mirror ``OllamaChat`` exactly — same synchronous
    signatures, same delta-streaming with optional token accounting — so the two
    providers stay visibly parallel and no extra dependency is needed.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        deployment: str | None = None,
        api_version: str | None = None,
    ) -> None:
        settings = get_settings()
        self.endpoint = (endpoint or settings.azure_openai_endpoint).rstrip("/")
        self.api_key = api_key or settings.azure_openai_api_key
        self.deployment = deployment or settings.azure_openai_deployment
        self.api_version = api_version or settings.azure_openai_api_version

    def _url(self) -> str:
        return (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/chat/completions?api-version={self.api_version}"
        )

    def _headers(self) -> dict[str, str]:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def _body(
        self,
        messages: list[Message],
        temperature: float,
        max_tokens: int | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Send a chat request and return the assistant message content."""
        response = httpx.post(
            self._url(),
            headers=self._headers(),
            json=self._body(messages, temperature, max_tokens, stream=False),
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return parse_azure_chat_response(response.json())

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        usage: Usage | None = None,
    ) -> Iterator[str]:
        """Stream the assistant response token-by-token (Server-Sent Events).

        Yields content deltas as they arrive. If a ``usage`` accumulator is passed,
        the token counts from the final ``usage`` chunk are added to it (Azure emits
        them because ``stream_options.include_usage`` is set).
        """
        with httpx.stream(
            "POST",
            self._url(),
            headers=self._headers(),
            json=self._body(messages, temperature, max_tokens, stream=True),
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {}).get("content", "")
                    if delta:
                        yield delta
                chunk_usage = chunk.get("usage")
                if chunk_usage and usage is not None:
                    usage.add(
                        Usage(
                            prompt_tokens=int(chunk_usage.get("prompt_tokens", 0)),
                            completion_tokens=int(chunk_usage.get("completion_tokens", 0)),
                        )
                    )


def make_chat_client(settings: Settings | None = None) -> ChatClient:
    """Return the chat client selected by ``LLM_PROVIDER`` (default: local Ollama)."""
    settings = settings or get_settings()
    provider = settings.llm_provider
    if provider == "ollama":
        return OllamaChat()
    if provider == "azure_openai":
        return AzureOpenAIChat()
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r} (expected 'ollama' or 'azure_openai')")
