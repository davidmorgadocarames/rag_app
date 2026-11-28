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
    AzureReasoningBudgetExhausted,
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


# --- AzureReasoningBudgetExhausted (14.7-migration-live, part 2e) --------------------------
#
# The FIRST real user run: gpt-5-mini's smoke `chat_json` call returned empty content with
# `finish_reason="length"` (its hidden reasoning tokens consumed the whole `max_completion_tokens`
# bound) -- json.loads("") raised JSONDecodeError twice, then chat_json's own retry+fallback
# silently swallowed it. This is a distinct, nameable outcome, not a generic malformed body.


def test_parse_azure_chat_response_raises_reasoning_budget_exhausted_on_empty_length_content() -> (
    None
):
    data = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    with pytest.raises(AzureReasoningBudgetExhausted):
        parse_azure_chat_response(data)


def test_azure_reasoning_budget_exhausted_is_a_value_error_with_a_fixed_safe_message() -> None:
    data = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    with pytest.raises(ValueError) as excinfo:
        parse_azure_chat_response(data)
    assert isinstance(excinfo.value, AzureReasoningBudgetExhausted)
    # fixed literal text -- never echoes the (empty) content or any request/response detail
    assert "max_completion_tokens" in str(excinfo.value)


def test_parse_azure_chat_response_returns_empty_content_when_finish_reason_is_not_length() -> None:
    """Empty content alone is not enough to raise -- only paired with finish_reason="length"
    (the actual reasoning-budget-exhaustion signature); any other finish_reason with empty
    content is returned as-is, unchanged from the pre-2e behaviour."""
    data = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    assert parse_azure_chat_response(data) == ""


def test_parse_azure_chat_response_non_empty_content_never_raises_with_finish_reason_length() -> (
    None
):
    data = {"choices": [{"message": {"content": "ok"}, "finish_reason": "length"}]}
    assert parse_azure_chat_response(data) == "ok"


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


# --- DEC-064 (eval-determinism): a fixed Ollama seed, always on ---------------------------


def test_ollama_chat_sends_the_configured_seed_in_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (`config.Settings.ollama_seed`, 42) is sent on every call -- this is
    what fixes the golden-set eval's run-to-run non-determinism (DEC-064), and is harmless
    in production since every caller already runs at temperature=0 (greedy)."""
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    client = OllamaChat(host="http://fake", model="m")
    client.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    assert captured["options"]["seed"] == 42


def test_ollama_chat_uses_the_configured_seed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_SEED", "7")
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    client = OllamaChat(host="http://fake", model="m")
    client.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    assert captured["options"]["seed"] == 7


def test_ollama_chat_omits_seed_from_options_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(ollama_seed=None))
    captured: dict[str, Any] = {}
    _patch_post(monkeypatch, captured)
    client = OllamaChat(host="http://fake", model="m")
    client.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    assert "seed" not in captured["options"]


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


# --- DEBT-036 migration prep: reasoning-model request shape (max_completion_tokens, no
# custom temperature) behind Settings.azure_openai_reasoning_model — off by default, so these
# tests would fail against the pre-migration-prep code (no such body-shape switch existed). ---


def test_azure_chat_non_reasoning_model_keeps_max_tokens_and_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (today's gpt-4.1-mini, non-reasoning): no behaviour change."""
    captured: dict[str, Any] = {}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.update(json)
        return _FakeHttpResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(endpoint="http://fake", api_key="k", deployment="d", api_version="v")
    assert client.reasoning_model is False
    client.chat([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=16)
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 16
    assert "max_completion_tokens" not in captured


def test_azure_chat_reasoning_model_sends_max_completion_tokens_and_omits_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reasoning-tier target model (o-series/gpt-5 family, DEBT-036) rejects both
    ``max_tokens`` and a custom ``temperature``: Azure's Chat Completions API returns a 400
    ("unsupported parameter") for either field on these deployments."""
    captured: dict[str, Any] = {}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.update(json)
        return _FakeHttpResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    client = AzureOpenAIChat(
        endpoint="http://fake",
        api_key="k",
        deployment="d",
        api_version="v",
        reasoning_model=True,
    )
    assert client.reasoning_model is True
    client.chat([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=16)
    assert captured["max_completion_tokens"] == 16
    assert "max_tokens" not in captured
    assert "temperature" not in captured


def test_azure_chat_reasoning_model_setting_defaults_the_constructor_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Settings.azure_openai_reasoning_model`` (not just the constructor kwarg) drives the
    body shape when the client is built with no explicit override — the same
    ``get_settings()`` fallback every other Azure client field already uses."""
    captured: dict[str, Any] = {}

    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> _FakeHttpResponse:
        captured.update(json)
        return _FakeHttpResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: Settings(
            llm_provider="azure_openai",
            azure_openai_endpoint="http://fake",
            azure_openai_api_key="k",
            azure_openai_deployment="d",
            azure_openai_reasoning_model=True,
        ),
    )
    client = make_chat_client()
    assert isinstance(client, AzureOpenAIChat)
    assert client.reasoning_model is True
    client.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    assert "max_completion_tokens" in captured
    assert "temperature" not in captured


# --- 14.7-migration-live part 2a: DA-14.7-migration-1, per-deployment API version. These
# tests would fail on the pre-change code (no such switch existed -- api_version always came
# from the single azure_openai_api_version field regardless of reasoning_model). ---


def test_azure_chat_non_reasoning_model_uses_the_plain_api_version() -> None:
    """The gpt-4.1-mini path is completely unaffected by the new reasoning-only field."""
    client = AzureOpenAIChat(
        endpoint="http://fake",
        api_key="k",
        deployment="d",
        reasoning_model=False,
    )
    settings = Settings()
    assert client.api_version == settings.azure_openai_api_version


def test_azure_chat_reasoning_model_uses_its_own_api_version_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-1: a reasoning-family deployment reads
    ``azure_openai_reasoning_api_version`` instead of ``azure_openai_api_version`` -- even
    when the two settings are given different values, so the switch is genuinely exercised
    (not merely passing because both fields share one default)."""
    settings = Settings(
        azure_openai_api_version="2024-10-21",
        azure_openai_reasoning_api_version="2024-12-01-preview",
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    client = AzureOpenAIChat(
        endpoint="http://fake",
        api_key="k",
        deployment="d",
        reasoning_model=True,
    )
    assert client.api_version == "2024-12-01-preview"
    assert client.api_version != settings.azure_openai_api_version


def test_azure_chat_explicit_api_version_kwarg_wins_over_either_setting() -> None:
    """An explicit ``api_version`` kwarg (e.g. a smoke script probing candidate versions)
    always wins, reasoning or not -- unchanged behaviour, guarded so a future refactor of the
    new branch cannot silently drop it."""
    client = AzureOpenAIChat(
        endpoint="http://fake",
        api_key="k",
        deployment="d",
        api_version="2025-03-01-preview",
        reasoning_model=True,
    )
    assert client.api_version == "2025-03-01-preview"
