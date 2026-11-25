"""Chat clients for the LLM tier.

Two interchangeable providers implement the same ``ChatClient`` interface:
``OllamaChat`` (local qwen, the free/dev default) and ``AzureOpenAIChat`` (the hosted
cloud provider). ``make_chat_client`` picks one from ``LLM_PROVIDER``. The HTTP call is
isolated from message building so callers/tests can construct prompts without a live
server.

``chat_json`` (T14.1.1/T14.1.2) is the structured-output variant both providers also
implement: Ollama via its ``format`` field pinned to the Pydantic model's JSON schema,
Azure OpenAI via ``response_format: {type: json_schema, strict: true}``. Both route
through the shared ``_chat_json_with_retry`` helper -- parse, validate against the
Pydantic model, one bounded retry, then the caller's safe ``fallback`` -- so a malformed
router/summary reply degrades gracefully instead of breaking the chat flow.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from rag_app.config import Settings, get_settings

_TIMEOUT_SECONDS = 180

Message = dict[str, str]

# T14.1.1: the Pydantic model type a ``chat_json`` caller validates its reply against
# (router output, conversation summary, ...). Bound to ``BaseModel`` so both providers'
# ``chat_json`` can call ``.model_json_schema()`` / ``.model_validate()`` generically.
SchemaT = TypeVar("SchemaT", bound=BaseModel)

# T11.3.4: an early warning for a prompt that may not fit the explicit `num_ctx` passed for
# its call type (answer/groundedness, config.py). Ollama truncates a request that does not
# fit `num_ctx` SILENTLY (no error, no HTTP status) instead of failing, so without this the
# bug would be invisible. This logger is never configured with its own handler: a
# `.warning()` call is at/above `logging.lastResort`'s threshold (WARNING), so it reaches
# stderr even though this app never calls `logging.basicConfig` (unlike `rag_app.timing`'s
# INFO-level records, which needed `install_timing_log()` — T11.3.1 finding DA-11bA, block A).
logger = logging.getLogger("rag_app.llm")

# Deliberately crude (chars / 4): good enough to catch an order-of-magnitude misconfiguration
# (an oversized context silently truncated by Ollama) as an early warning, without adding a
# real tokenizer dependency just to log one.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _estimate_prompt_tokens(messages: list[Message]) -> int:
    return sum(len(m.get("content", "")) for m in messages) // _CHARS_PER_TOKEN_ESTIMATE


def _warn_if_may_overflow(
    messages: list[Message], *, max_tokens: int | None, num_ctx: int | None, call_type: str
) -> None:
    """Log (never raise — Ollama itself does not) when a prompt may not fit ``num_ctx``.

    No PII (same discipline as ``rag_app.timing``): only the call type and token counts,
    never the message content.
    """
    if num_ctx is None:
        return
    estimated = _estimate_prompt_tokens(messages) + (max_tokens or 0)
    if estimated > num_ctx:
        logger.warning(
            "llm context may overflow num_ctx: call_type=%s estimated_tokens~%d num_ctx=%d"
            " (Ollama truncates context silently instead of failing — raise num_ctx or"
            " shrink the prompt)",
            call_type,
            estimated,
            num_ctx,
        )


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
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> str: ...

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        usage: Usage | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> Iterator[str]: ...

    def chat_json(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        fallback: SchemaT,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> SchemaT: ...


def _strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """``schema.model_json_schema()`` with ``additionalProperties: false`` set on every
    object sub-schema (top level and any nested ``$defs``) -- required by Azure OpenAI's
    ``strict: true`` structured-output mode, which Pydantic does not set on its own."""
    raw = schema.model_json_schema()

    def _tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node.setdefault("additionalProperties", False)
            for value in node.values():
                _tighten(value)
        elif isinstance(node, list):
            for item in node:
                _tighten(item)

    _tighten(raw)
    return raw


def _validate_json_content(content: str, schema: type[SchemaT]) -> SchemaT:
    """Parse ``content`` as JSON and validate it against ``schema``; raises
    ``json.JSONDecodeError``/``pydantic.ValidationError`` on failure (caught by
    ``_chat_json_with_retry``)."""
    data = json.loads(content)
    return schema.model_validate(data)


def _chat_json_with_retry(
    perform_call: Callable[[], str],
    schema: type[SchemaT],
    fallback: SchemaT,
    *,
    call_type: str,
) -> SchemaT:
    """T14.1.2: validate the structured reply, one bounded retry, then ``fallback``.

    Invalid JSON or a schema violation on the first attempt is retried once (two
    attempts total); a second failure returns the caller's safe ``fallback`` rather
    than raising -- a malformed router/summary reply must never break the chat flow.
    No prompt/response content is logged (same discipline as the rest of this module),
    only the call type, schema name and error class."""
    attempts = 2
    for attempt in range(1, attempts + 1):
        content = perform_call()
        try:
            return _validate_json_content(content, schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "chat_json invalid output (attempt %d/%d): call_type=%s schema=%s error=%s",
                attempt,
                attempts,
                call_type,
                schema.__name__,
                type(exc).__name__,
            )
    logger.warning(
        "chat_json falling back to the caller's default: call_type=%s schema=%s",
        call_type,
        schema.__name__,
    )
    return fallback


class OllamaChat:
    """Minimal client for the Ollama chat endpoint."""

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.llm_model
        self.keep_alive = settings.ollama_keep_alive

    def _options(
        self, temperature: float, max_tokens: int | None, num_ctx: int | None
    ) -> dict[str, float | int]:
        options: dict[str, float | int] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if num_ctx is not None:
            options["num_ctx"] = num_ctx
        return options

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> str:
        """Send a chat request and return the assistant message content.

        ``num_ctx`` (T11.3.4) is explicit per call type; a request without it falls back to
        Ollama's own default (2048) rather than the currently loaded model's context —
        callers that need to stay on the same loaded context (config.py's note on
        ``num_ctx_answer``/``num_ctx_groundedness``) must always pass the same value.
        """
        _warn_if_may_overflow(messages, max_tokens=max_tokens, num_ctx=num_ctx, call_type=call_type)
        response = httpx.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": self._options(temperature, max_tokens, num_ctx),
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
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> Iterator[str]:
        """Stream the assistant response token-by-token.

        Yields content deltas as they arrive. If a ``usage`` accumulator is passed,
        the final token counts (``prompt_eval_count`` / ``eval_count``) are added to
        it once the stream completes. See ``chat`` for ``num_ctx``/``call_type``.
        """
        _warn_if_may_overflow(messages, max_tokens=max_tokens, num_ctx=num_ctx, call_type=call_type)
        with httpx.stream(
            "POST",
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": True,
                "keep_alive": self.keep_alive,
                "options": self._options(temperature, max_tokens, num_ctx),
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

    def chat_json(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        fallback: SchemaT,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> SchemaT:
        """Structured-output chat call (T14.1.1): Ollama's ``format`` field pinned to
        ``schema``'s JSON schema. ``num_ctx``/overflow-warning semantics match ``chat``
        (future router/summary callers that share the answer/groundedness keep-alive
        window must pin ``num_ctx`` the same way ``agentic.reformulate`` and
        ``eval.judge.judge_correctness`` already do -- see ``test_llm_bounds.py``). See
        module-level ``_chat_json_with_retry`` for the validate/retry/fallback contract
        (T14.1.2)."""
        _warn_if_may_overflow(messages, max_tokens=max_tokens, num_ctx=num_ctx, call_type=call_type)
        json_schema = schema.model_json_schema()

        def _call() -> str:
            response = httpx.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "format": json_schema,
                    "options": self._options(temperature, max_tokens, num_ctx),
                },
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content = response.json()["message"]["content"]
            if not isinstance(content, str):
                raise ValueError(f"unexpected chat response: {content!r}")
            return content

        return _chat_json_with_retry(_call, schema, fallback, call_type=call_type)


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

    def _json_body(
        self,
        messages: list[Message],
        temperature: float,
        max_tokens: int | None,
        schema: type[BaseModel],
    ) -> dict[str, Any]:
        body = self._body(messages, temperature, max_tokens, stream=False)
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": _strict_json_schema(schema),
                "strict": True,
            },
        }
        return body

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> str:
        """Send a chat request and return the assistant message content.

        ``num_ctx``/``call_type`` are accepted (the same ``ChatClient`` shape as
        ``OllamaChat``) but unused: Azure OpenAI's context window is fixed by the deployment,
        not by a per-request option (T11.3.4 — see config.py's ``num_ctx_answer`` note).
        """
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
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> Iterator[str]:
        """Stream the assistant response token-by-token (Server-Sent Events).

        Yields content deltas as they arrive. If a ``usage`` accumulator is passed,
        the token counts from the final ``usage`` chunk are added to it (Azure emits
        them because ``stream_options.include_usage`` is set). ``num_ctx``/``call_type``:
        see ``chat``.
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

    def chat_json(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        fallback: SchemaT,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        call_type: str = "unknown",
    ) -> SchemaT:
        """Structured-output chat call (T14.1.1): Azure OpenAI's
        ``response_format: {type: json_schema, strict: true}``, with
        ``additionalProperties: false`` added to every object sub-schema (see
        ``_strict_json_schema`` -- Azure's strict mode requires it, Pydantic does not
        set it). ``num_ctx``/``call_type`` accepted and unused for ``num_ctx`` -- see
        ``chat``. See module-level ``_chat_json_with_retry`` for the validate/retry/
        fallback contract (T14.1.2)."""

        def _call() -> str:
            response = httpx.post(
                self._url(),
                headers=self._headers(),
                json=self._json_body(messages, temperature, max_tokens, schema),
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return parse_azure_chat_response(response.json())

        return _chat_json_with_retry(_call, schema, fallback, call_type=call_type)


def make_chat_client(settings: Settings | None = None) -> ChatClient:
    """Return the chat client selected by ``LLM_PROVIDER`` (default: local Ollama)."""
    settings = settings or get_settings()
    provider = settings.llm_provider
    if provider == "ollama":
        return OllamaChat()
    if provider == "azure_openai":
        return AzureOpenAIChat()
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r} (expected 'ollama' or 'azure_openai')")
