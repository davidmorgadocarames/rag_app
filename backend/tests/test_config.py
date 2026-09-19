"""Smoke tests for configuration loading.

These give CI something real to run from day 1 and guard the config contract.
"""

from rag_app import __version__
from rag_app.config import Settings, get_settings


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_settings_load_with_defaults() -> None:
    settings = get_settings()
    assert isinstance(settings, Settings)


def test_three_model_contract() -> None:
    """The system deliberately uses exactly three models."""
    settings = get_settings()
    assert "qwen" in settings.llm_model.lower()
    assert settings.embed_model == "bge-m3"
    assert "bge-reranker" in settings.reranker_model.lower()


def test_retrieval_limits_are_sane() -> None:
    settings = get_settings()
    assert settings.rerank_top_n <= settings.top_k
    assert settings.max_agent_steps > 0
