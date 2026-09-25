"""Unit tests for the LLM provider layer (no live Ollama/Azure required).

Covers the pure Azure response parser and the provider factory that ``LLM_PROVIDER``
selects; neither test makes a network call.
"""

from __future__ import annotations

import pytest

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
