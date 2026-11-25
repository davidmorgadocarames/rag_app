"""Unit tests for ``chat_json`` (T14.1.1/T14.1.2): structured output for both LLM
providers, mocked -- no live Ollama/Azure OpenAI call (cost, per the orchestrator's
prompt).

Covers: Ollama's ``format`` field pinned to the Pydantic schema; Azure's
``response_format: {type: json_schema, strict: true}`` with ``additionalProperties:
false`` added (including nested ``$defs``); the shared validate/retry/fallback contract
(one bounded retry, then the caller's safe fallback) for both invalid JSON and a schema
violation; ``num_ctx``/``call_type`` plumbing identical to ``chat``/``chat_stream``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from rag_app import llm
from rag_app.llm import AzureOpenAIChat, OllamaChat, _strict_json_schema


class _RouterOutput(BaseModel):
    intent: str
    language: str


_FALLBACK = _RouterOutput(intent="security", language="en")


class _Nested(BaseModel):
    topic: str


class _WithNested(BaseModel):
    items: list[_Nested]


# --- _strict_json_schema (pure function, no mocking) -----------------------------------------


def test_strict_json_schema_sets_additional_properties_false_on_top_level() -> None:
    schema = _strict_json_schema(_RouterOutput)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_strict_json_schema_recurses_into_defs() -> None:
    """A nested model's own object schema (in ``$defs``) must also be tightened --
    Azure's strict mode rejects a schema where only the outer object is tightened."""
    schema = _strict_json_schema(_WithNested)
    assert schema["additionalProperties"] is False
    nested = schema["$defs"]["_Nested"]
    assert nested["additionalProperties"] is False


def test_strict_json_schema_does_not_mutate_pydantics_own_cache() -> None:
    """``model_json_schema()`` can return a cached dict; calling ``_strict_json_schema``
    twice must not accumulate state or raise."""
    first = _strict_json_schema(_RouterOutput)
    second = _strict_json_schema(_RouterOutput)
    assert first == second


# --- Fake HTTP plumbing (same pattern as test_llm.py) ----------------------------------------


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _ollama_payload(content: str) -> dict[str, Any]:
    return {"message": {"content": content}}


def _azure_payload(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


# --- OllamaChat.chat_json ---------------------------------------------------------------------


def test_ollama_chat_json_happy_path_returns_validated_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.append(json)
        return _FakeHttpResponse(_ollama_payload('{"intent": "chitchat", "language": "en"}'))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = OllamaChat(host="http://fake", model="m")
    result = client.chat_json(
        [{"role": "user", "content": "hi"}],
        _RouterOutput,
        fallback=_FALLBACK,
        max_tokens=64,
        num_ctx=8192,
        call_type="router",
    )
    assert result == _RouterOutput(intent="chitchat", language="en")
    assert len(captured) == 1
    body = captured[0]
    assert body["format"] == _RouterOutput.model_json_schema()
    assert body["options"]["num_ctx"] == 8192
    assert body["options"]["num_predict"] == 64


def test_ollama_chat_json_retries_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            content = "not json at all"
        else:
            content = '{"intent": "security", "language": "en"}'
        return _FakeHttpResponse(_ollama_payload(content))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = OllamaChat(host="http://fake", model="m")
    result = client.chat_json(
        [{"role": "user", "content": "hi"}], _RouterOutput, fallback=_FALLBACK, max_tokens=64
    )
    assert calls["n"] == 2  # the retry really made a second HTTP call
    assert result == _RouterOutput(intent="security", language="en")


def test_ollama_chat_json_falls_back_after_invalid_json_twice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        calls["n"] += 1
        return _FakeHttpResponse(_ollama_payload("still not json"))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    caplog.set_level(logging.WARNING, logger="rag_app.llm")
    client = OllamaChat(host="http://fake", model="m")
    result = client.chat_json(
        [{"role": "user", "content": "hi"}],
        _RouterOutput,
        fallback=_FALLBACK,
        max_tokens=64,
        call_type="router",
    )
    assert calls["n"] == 2  # bounded: exactly one retry, never an unbounded loop
    assert result is _FALLBACK
    messages = [r.getMessage() for r in caplog.records if r.name == "rag_app.llm"]
    assert any("call_type=router" in m for m in messages)
    assert any("falling back" in m for m in messages)


def test_ollama_chat_json_falls_back_on_a_schema_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JSON that does not satisfy the Pydantic schema (missing ``language``) is
    rejected exactly like invalid JSON -- never returned as-is."""
    calls = {"n": 0}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        calls["n"] += 1
        return _FakeHttpResponse(_ollama_payload('{"intent": "security"}'))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = OllamaChat(host="http://fake", model="m")
    result = client.chat_json(
        [{"role": "user", "content": "hi"}], _RouterOutput, fallback=_FALLBACK, max_tokens=64
    )
    assert calls["n"] == 2
    assert result is _FALLBACK


# --- AzureOpenAIChat.chat_json ------------------------------------------------------------------


def test_azure_chat_json_request_body_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.append(json)
        return _FakeHttpResponse(_azure_payload('{"intent": "chitchat", "language": "en"}'))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(endpoint="http://fake", api_key="k", deployment="d", api_version="v")
    client.chat_json(
        [{"role": "user", "content": "hi"}],
        _RouterOutput,
        fallback=_FALLBACK,
        max_tokens=64,
        num_ctx=8192,  # must be accepted and ignored, like chat()
        call_type="router",
    )
    assert len(captured) == 1
    body = captured[0]
    assert "num_ctx" not in body  # never sent to Azure (T11.3.4 behaviour, unchanged)
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "_RouterOutput"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert response_format["json_schema"]["schema"] == _strict_json_schema(_RouterOutput)


def test_azure_chat_json_happy_path_returns_validated_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        return _FakeHttpResponse(_azure_payload('{"intent": "security", "language": "es"}'))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(endpoint="http://fake", api_key="k", deployment="d", api_version="v")
    result = client.chat_json(
        [{"role": "user", "content": "hi"}], _RouterOutput, fallback=_FALLBACK, max_tokens=64
    )
    assert result == _RouterOutput(intent="security", language="es")


def test_azure_chat_json_falls_back_after_invalid_json_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        calls["n"] += 1
        return _FakeHttpResponse(_azure_payload("<<not json>>"))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(endpoint="http://fake", api_key="k", deployment="d", api_version="v")
    result = client.chat_json(
        [{"role": "user", "content": "hi"}], _RouterOutput, fallback=_FALLBACK, max_tokens=64
    )
    assert calls["n"] == 2
    assert result is _FALLBACK
