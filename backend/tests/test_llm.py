"""Unit tests for the LLM provider layer (no live Ollama/Azure required).

Covers the pure Azure response parser and the provider factory that ``LLM_PROVIDER``
selects; neither test makes a network call.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from rag_app import llm
from rag_app.config import Settings
from rag_app.llm import (
    AzureOpenAIChat,
    OllamaChat,
    make_chat_client,
    parse_azure_chat_response,
)


def test_parse_azure_chat_response_returns_content() -> None:
    data = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    assert parse_azure_chat_response(data) == "hello"


def test_parse_azure_chat_response_rejects_missing_choices() -> None:
    with pytest.raises(ValueError, match="azure"):
        parse_azure_chat_response({"error": "bad request"})


def test_parse_azure_chat_response_rejects_empty_choices() -> None:
    with pytest.raises(ValueError, match="azure"):
        parse_azure_chat_response({"choices": []})


def test_parse_azure_chat_response_rejects_non_string_content() -> None:
    with pytest.raises(ValueError, match="azure"):
        parse_azure_chat_response({"choices": [{"message": {"content": None}}]})


def test_make_chat_client_defaults_to_ollama() -> None:
    client = make_chat_client(Settings(llm_provider="ollama"))
    assert isinstance(client, OllamaChat)


def test_make_chat_client_selects_azure() -> None:
    client = make_chat_client(Settings(llm_provider="azure_openai"))
    assert isinstance(client, AzureOpenAIChat)


def test_make_chat_client_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        make_chat_client(Settings(llm_provider="gpt5"))


# --- T11.3.4: num_ctx sent to Ollama; overflow is logged, never raised ----------------------


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_post(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.update(json)
        return _FakeHttpResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(llm.httpx, "post", fake_post)


def test_ollama_chat_sends_the_explicit_num_ctx_in_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    client = OllamaChat(host="http://fake", model="m")
    client.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=16,
        num_ctx=4096,
        call_type="groundedness",
    )
    assert captured["options"]["num_ctx"] == 4096


def test_ollama_chat_omits_num_ctx_from_options_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router/judge calls (deferred by T11.3.4) do not pass ``num_ctx`` — Ollama then uses
    its own default rather than an explicit value; the key must be absent, not ``null``."""
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    client = OllamaChat(host="http://fake", model="m")
    client.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    assert "num_ctx" not in captured["options"]


def test_ollama_chat_warns_when_the_prompt_may_overflow_num_ctx(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    caplog.set_level(logging.WARNING, logger="rag_app.llm")
    client = OllamaChat(host="http://fake", model="m")
    big_context = "x" * 20_000  # ~5000 estimated tokens (chars / 4)
    client.chat(
        [{"role": "user", "content": big_context}],
        max_tokens=1024,
        num_ctx=2048,
        call_type="answer",
    )
    warnings = [r for r in caplog.records if r.name == "rag_app.llm"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "call_type=answer" in message
    assert "num_ctx=2048" in message
    assert big_context not in message  # no PII / prompt content in the log


def test_ollama_chat_does_not_warn_when_num_ctx_is_ample(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    caplog.set_level(logging.WARNING, logger="rag_app.llm")
    client = OllamaChat(host="http://fake", model="m")
    client.chat(
        [{"role": "user", "content": "a short question"}],
        max_tokens=1024,
        num_ctx=8192,
        call_type="answer",
    )
    assert [r for r in caplog.records if r.name == "rag_app.llm"] == []


def test_ollama_chat_never_warns_without_an_explicit_num_ctx(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``num_ctx`` (router/judge, deferred): nothing to overflow-check against."""
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    caplog.set_level(logging.WARNING, logger="rag_app.llm")
    client = OllamaChat(host="http://fake", model="m")
    client.chat([{"role": "user", "content": "x" * 50_000}], max_tokens=1024)
    assert [r for r in caplog.records if r.name == "rag_app.llm"] == []


def test_azure_chat_accepts_and_ignores_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Azure OpenAI's context window is fixed by the deployment (T11.3.4)."""
    captured: dict[str, Any] = {}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.update(json)
        return _FakeHttpResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(endpoint="http://fake", api_key="k", deployment="d", api_version="v")
    content = client.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=16,
        num_ctx=4096,
        call_type="answer",
    )
    assert content == "ok"
    assert "num_ctx" not in captured  # never sent to Azure at all
