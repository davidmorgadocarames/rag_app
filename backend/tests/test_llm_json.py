"""Unit tests for ``chat_json`` (T14.1.1/T14.1.2): structured output for both LLM
providers, mocked -- no live Ollama/Azure OpenAI call (cost, per the orchestrator's
prompt).

Covers: Ollama's ``format`` field pinned to the plain, untightened Pydantic schema;
Azure's ``response_format: {type: json_schema, strict: true}`` with
``additionalProperties: false`` AND every property (including ``Optional`` ones with a
default) listed in ``required`` on every object node -- top level, nested ``$defs`` and
array ``items`` alike (DA-14-B1-1 fix: Azure's ``strict: true`` mode rejects a schema
where any property is missing from ``required``; optionality is expressed only via a
nullable type, not by omission); the shared validate/retry/fallback contract (one
bounded retry, then the caller's safe fallback) for both invalid JSON and a schema
violation; ``num_ctx``/``call_type`` plumbing identical to ``chat``/``chat_stream``.
"""

from __future__ import annotations

import enum
import logging
from typing import Any

import pytest
from pydantic import BaseModel

from rag_app import llm
from rag_app.llm import (
    AzureOpenAIChat,
    AzureReasoningBudgetExhausted,
    OllamaChat,
    _strict_json_schema,
)


class _RouterOutput(BaseModel):
    intent: str
    language: str


_FALLBACK = _RouterOutput(intent="security", language="en")


class _Nested(BaseModel):
    topic: str


class _WithNested(BaseModel):
    items: list[_Nested]


class _Mood(str, enum.Enum):
    """An enum field: its own ``$defs`` sub-schema has no ``properties`` key, so the
    object-tightening branch must not touch it (and must not raise on it either)."""

    HAPPY = "happy"
    SAD = "sad"


class _WithOptionalNestedListAndEnum(BaseModel):
    """DA-14-B1-1 regression fixture: one required field, one ``Optional`` field *with
    a default* (the case Pydantic never puts in its own ``required``), a nested model,
    a list of nested models, and an enum -- every object node in the resulting schema
    (top level, the nested ``$defs`` entry, and the list's ``items``) must end up with
    ``required`` covering every one of its ``properties`` keys."""

    name: str
    note: str | None = None
    mood: _Mood
    nested: _Nested
    items: list[_Nested]


def _object_nodes(node: Any) -> list[dict[str, Any]]:
    """Recursively collect every object sub-schema (``type == "object"`` with a
    ``properties`` key) reachable from ``node`` -- top level, nested ``properties``,
    ``$defs`` and array ``items`` alike."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(_object_nodes(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_object_nodes(item))
    return found


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


def test_strict_json_schema_does_not_mutate_the_callers_schema_dict() -> None:
    """``_strict_json_schema`` must operate on a copy: mutating its returned dict (or
    the dict returned by a prior call) must never leak into a fresh
    ``model_json_schema()``/``_strict_json_schema()`` call on the same model."""
    raw_before = _RouterOutput.model_json_schema()
    tightened = _strict_json_schema(_RouterOutput)
    tightened["additionalProperties"] = "mutated"
    tightened["required"].append("mutated-field")
    # the caller's own mutation of the returned dict must not affect either the raw
    # Pydantic schema or a fresh call to _strict_json_schema.
    assert _RouterOutput.model_json_schema() == raw_before
    assert _strict_json_schema(_RouterOutput)["additionalProperties"] is False
    assert _strict_json_schema(_RouterOutput)["required"] == ["intent", "language"]


def test_strict_json_schema_required_covers_every_property_everywhere() -> None:
    """DA-14-B1-1: Azure's ``strict: true`` mode rejects a schema where any property
    (including an ``Optional`` one with a default) is missing from its object's
    ``required`` array -- optionality must be expressed only via a nullable type.
    Every object node reachable from the tightened schema (top level, the nested
    ``$defs`` entry, and the list's ``items``) must list every one of its own
    ``properties`` keys in ``required``, with ``additionalProperties: false``."""
    schema = _strict_json_schema(_WithOptionalNestedListAndEnum)

    object_nodes = _object_nodes(schema)
    # top level + the one nested _Nested $defs entry (list items reference $defs by
    # $ref, so they do not duplicate a separate "items" object node here) == 2.
    assert len(object_nodes) == 2
    for obj in object_nodes:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"].keys())

    # explicitly: the optional field (a default, never in Pydantic's own "required")
    # is still listed in the top-level schema's "required".
    assert "note" in schema["required"]
    assert set(schema["required"]) == set(schema["properties"].keys())

    # the enum's own $defs entry has no "properties" key, so it is correctly not an
    # "object node" at all -- never touched, never raises.
    mood_def = schema["$defs"]["_Mood"]
    assert "properties" not in mood_def
    assert "required" not in mood_def


def test_strict_json_schema_optional_field_still_validates_as_null() -> None:
    """The ``required``-array fix changes only the *schema advertised to Azure*, never
    runtime validation: a model with an ``Optional`` field still validates a response
    where that field is ``null`` (or omitted), via ``model_validate`` -- exactly how
    ``_chat_json_with_retry`` consumes a provider's JSON reply."""
    _strict_json_schema(_WithOptionalNestedListAndEnum)  # schema generation must not raise
    data_with_null = {
        "name": "a",
        "note": None,
        "mood": "happy",
        "nested": {"topic": "t"},
        "items": [{"topic": "t2"}],
    }
    result = _WithOptionalNestedListAndEnum.model_validate(data_with_null)
    assert result.note is None

    data_omitted = {k: v for k, v in data_with_null.items() if k != "note"}
    result_omitted = _WithOptionalNestedListAndEnum.model_validate(data_omitted)
    assert result_omitted.note is None


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


def test_ollama_chat_json_sends_the_untightened_schema_even_with_an_optional_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14-B1-1 scope check: the ``required``-array fix is Azure-only.
    ``OllamaChat.chat_json`` must keep sending the plain, untightened
    ``schema.model_json_schema()`` as-is -- Ollama's ``format`` field has no
    ``strict: true`` requirement, so a field with a default correctly stays absent
    from ``required`` there."""
    captured: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.append(json)
        return _FakeHttpResponse(
            _ollama_payload(
                '{"name": "a", "note": null, "mood": "happy",'
                ' "nested": {"topic": "t"}, "items": []}'
            )
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = OllamaChat(host="http://fake", model="m")
    fallback = _WithOptionalNestedListAndEnum(
        name="fb", mood=_Mood.SAD, nested=_Nested(topic="fb"), items=[]
    )
    client.chat_json(
        [{"role": "user", "content": "hi"}],
        _WithOptionalNestedListAndEnum,
        fallback=fallback,
        max_tokens=64,
    )
    assert len(captured) == 1
    raw_schema = _WithOptionalNestedListAndEnum.model_json_schema()
    assert captured[0]["format"] == raw_schema
    # the untightened schema does NOT list "note" in its top-level "required" --
    # confirms this request body is genuinely unaffected by the Azure-side fix.
    assert "note" not in raw_schema["required"]


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


def test_azure_chat_json_raises_reasoning_budget_exhausted_without_retrying_or_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """14.7-migration-live part 2e: the FIRST real user run's exact failure mode -- a reasoning
    deployment's hidden reasoning tokens consumed the whole bound, returning empty content with
    ``finish_reason="length"``. Unlike a plain malformed-JSON body (retried once, then a safe
    fallback), this is a DISTINCT, nameable outcome that propagates immediately -- retrying would
    not help (same fixed bound, same outcome) and callers that care (``run_smoke``) need to tell
    it apart from a generic JSON error rather than have it silently masked by the fallback."""
    calls = {"n": 0}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        calls["n"] += 1
        return _FakeHttpResponse(
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(
        endpoint="http://fake", api_key="k", deployment="d", api_version="v", reasoning_model=True
    )
    with pytest.raises(AzureReasoningBudgetExhausted):
        client.chat_json(
            [{"role": "user", "content": "hi"}], _RouterOutput, fallback=_FALLBACK, max_tokens=32
        )
    assert calls["n"] == 1  # no retry -- a second identical call would not help


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


def test_azure_chat_json_reasoning_model_sends_max_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEBT-036 migration prep: a reasoning-tier target model (``Settings
    .azure_openai_reasoning_model``) uses ``max_completion_tokens`` and no ``temperature``
    on ``chat_json`` too -- ``_body``/``_json_body`` share the same switch ``chat``/
    ``chat_stream`` use. The ``response_format``/``strict`` shape is unaffected."""
    captured: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.append(json)
        return _FakeHttpResponse(_azure_payload('{"intent": "chitchat", "language": "en"}'))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(
        endpoint="http://fake",
        api_key="k",
        deployment="d",
        api_version="v",
        reasoning_model=True,
    )
    client.chat_json(
        [{"role": "user", "content": "hi"}],
        _RouterOutput,
        fallback=_FALLBACK,
        max_tokens=64,
        call_type="router",
    )
    assert len(captured) == 1
    body = captured[0]
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body
    assert "temperature" not in body
    # strict-schema shape unaffected by the reasoning-model body switch
    assert body["response_format"]["json_schema"]["strict"] is True


def test_azure_chat_json_request_body_requires_the_optional_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14-B1-1: the request body Azure actually receives must list the optional
    field in ``required`` (Azure's ``strict: true`` would otherwise reject the
    schema), and a response where that field is ``null`` must still validate --
    without any fallback/retry."""
    captured: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.append(json)
        return _FakeHttpResponse(
            _azure_payload(
                '{"name": "a", "note": null, "mood": "happy",'
                ' "nested": {"topic": "t"}, "items": []}'
            )
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(endpoint="http://fake", api_key="k", deployment="d", api_version="v")
    fallback = _WithOptionalNestedListAndEnum(
        name="fb", mood=_Mood.SAD, nested=_Nested(topic="fb"), items=[]
    )
    result = client.chat_json(
        [{"role": "user", "content": "hi"}],
        _WithOptionalNestedListAndEnum,
        fallback=fallback,
        max_tokens=64,
    )
    assert len(captured) == 1  # no retry -- the null optional field validates first try
    sent_schema = captured[0]["response_format"]["json_schema"]["schema"]
    assert "note" in sent_schema["required"]
    assert result.note is None
    assert result != fallback


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
