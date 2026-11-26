"""Unit tests for per-answer token cost estimation (T14.5.4).

No DB/live LLM needed -- a plain ``Settings()`` instance (every field has a sane default,
same pattern ``test_db.py`` already uses) and the pure :func:`estimate_cost` function.
"""

from __future__ import annotations

from rag_app.config import Settings
from rag_app.cost import estimate_cost


def test_ollama_is_always_free_regardless_of_token_counts() -> None:
    """Fails on a hypothetical "always price it" implementation: local Ollama must never
    cost anything, no matter how many tokens were used."""
    settings = Settings()
    estimate = estimate_cost(
        "ollama", prompt_tokens=100_000, completion_tokens=50_000, settings=settings
    )
    assert estimate.amount == 0.0
    assert estimate.currency == "EUR"


def test_an_unknown_provider_is_also_free_not_an_error() -> None:
    settings = Settings()
    estimate = estimate_cost(
        "some_future_provider", prompt_tokens=1000, completion_tokens=1000, settings=settings
    )
    assert estimate.amount == 0.0


def test_azure_openai_cost_scales_with_tokens_using_the_configured_prices() -> None:
    settings = Settings(
        azure_openai_cost_per_1k_input_tokens=0.001,
        azure_openai_cost_per_1k_output_tokens=0.002,
    )
    estimate = estimate_cost(
        "azure_openai", prompt_tokens=2000, completion_tokens=1000, settings=settings
    )
    # 2000/1000 * 0.001 + 1000/1000 * 0.002 = 0.002 + 0.002 = 0.004
    assert estimate.amount == 0.004
    assert estimate.currency == "EUR"


def test_azure_openai_zero_tokens_is_zero_cost() -> None:
    settings = Settings()
    estimate = estimate_cost(
        "azure_openai", prompt_tokens=0, completion_tokens=0, settings=settings
    )
    assert estimate.amount == 0.0


def test_cost_currency_is_configurable() -> None:
    settings = Settings(cost_currency="USD")
    estimate = estimate_cost("ollama", prompt_tokens=10, completion_tokens=10, settings=settings)
    assert estimate.currency == "USD"
