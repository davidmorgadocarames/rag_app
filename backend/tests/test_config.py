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


# --- T13.4.1/T13.4.2 (TF14, DA-13-C-4): admin_live_mode -----------------------------------


def test_admin_live_mode_defaults_to_polling_the_azure_safe_value(monkeypatch) -> None:
    """Same shape as `prometheus_url`'s own empty-by-default: the Azure-safe choice IS the
    default, so a Container App that forgets to set anything stays safe (no long-lived SSE
    connection against internal ingress + scale-to-zero) -- `docker-compose.yml`/
    `scripts/gate.sh` explicitly opt INTO "sse" for the local stack instead."""
    monkeypatch.delenv("ADMIN_LIVE_MODE", raising=False)
    assert get_settings().admin_live_mode == "polling"


def test_admin_live_mode_can_be_set_to_sse(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_LIVE_MODE", "sse")
    assert get_settings().admin_live_mode == "sse"
