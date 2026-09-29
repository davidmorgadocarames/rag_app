"""Typed application configuration.

All configuration is loaded from environment variables (or a local `.env` file).
Nothing sensitive is hardcoded; see `.env.example` for the full list of keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only features (PHASE_PLANNING §1 rule 2, "fail-closed flags"): fake LLM
# provider (Phase 16), defence lab (13), data explorer (19), code-fix (20). Each one is a
# boolean Settings field declared with `dev_only_flag(...)`; the registry is DERIVED from that
# field metadata (DA-C-5), so there is no separate list to forget. With ENV=prod (the
# default) the API refuses to start while any of them is on. As a backstop, a boolean field
# whose NAME looks development-only (DEV_ONLY_NAME) counts as dev-only even when it was not
# declared that way, and a unit test fails on it. A development-only VALUE of a non-boolean
# field (e.g. a future LLM_PROVIDER=fake) needs its own validator in that phase.
DEV_ONLY_MARK = "dev_only"
DEV_ONLY_NAME = re.compile(
    r"(^|_)(fake|mock|stub|lab|labs|explorer|code_fix|codefix|dev|debug|demo|unsafe|insecure)(_|$)"
)


def dev_only_flag(description: str) -> Any:
    """A development-only boolean Settings field: off by default, refused with ENV=prod."""
    return Field(default=False, description=description, json_schema_extra={DEV_ONLY_MARK: True})


class DevOnlyFlagInProdError(RuntimeError):
    """A development-only feature is enabled while ENV=prod."""


class Settings(BaseSettings):
    """Application settings, populated from the environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Environment (T11.0.10) ---
    # "prod" unless ENV says otherwise: a missing or forgotten ENV can never switch on a
    # development-only feature. The local .env sets ENV=dev; Azure never sets it.
    env: Literal["dev", "prod"] = "prod"

    # --- Database (PostgreSQL + pgvector) ---
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3_driver(cls, v: str) -> str:
        """Force the psycopg (v3) driver on a bare Postgres URL.

        A cloud connection string like ``postgresql://user:pass@host/db`` carries no
        ``+driver``, so SQLAlchemy would default to psycopg2 — which we don't ship (the
        project is psycopg v3 only). Rewriting the scheme here fixes both the app engine
        and the Alembic migration engine, so the container can run its own migrations.
        Any explicit driver (e.g. ``+psycopg``, ``+asyncpg``) is left untouched.
        """
        for scheme in ("postgresql://", "postgres://"):
            if v.startswith(scheme):
                return "postgresql+psycopg://" + v[len(scheme) :]
        return v

    # --- Ollama / models (only 3 models across the whole system) ---
    ollama_host: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    embed_model: str = "bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # Keep the model resident in VRAM between requests (avoids a cold ~5 GB reload
    # per idle window). Ollama accepts a duration ("30m") or -1 to keep forever.
    ollama_keep_alive: str = "30m"

    # --- LLM provider selection ---
    # "ollama" (local/free, the dev default) or "azure_openai" (hosted, cloud path).
    # The Azure_* keys are only read when llm_provider == "azure_openai"; see ADR phase 10.
    llm_provider: str = "ollama"
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"

    # --- Ingestion / chunking ---
    chunk_size: int = 1200
    chunk_overlap: int = 150

    # --- Retrieval / agent limits ---
    top_k: int = 20
    rerank_top_n: int = 4
    max_agent_steps: int = 6
    max_tokens: int = 1024

    # --- Agentic router ---
    # Best rerank (cross-encoder) score below this is considered "thin" -> reformulate+retry.
    thin_threshold: float = 0.5
    max_query_rewrites: int = 1

    # --- Rate limiting (token bucket) ---
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0
    rate_limit_chat_cost: float = 5.0  # cost-aware: an LLM answer costs more than 1 token

    # --- Auth / security ---
    jwt_secret: str = ""
    jwt_expires_minutes: int = 30
    # Master key (urlsafe base64, 32 bytes) that wraps per-user data keys. Set in .env.
    data_master_key: str = ""
    require_email_verification: bool = False  # gate login on a verified email when true

    # --- Email verification (SMTP) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@example.com"

    # --- Frontend ---
    next_public_api_url: str = Field(default="http://localhost:8000")
    frontend_origin: str = "http://localhost:3000"  # CORS: allow the browser app


def get_settings() -> Settings:
    """Return the application settings loaded from the environment."""
    return Settings()


def _is_marked_dev_only(extra: object) -> bool:
    return isinstance(extra, dict) and extra.get(DEV_ONLY_MARK) is True


def dev_only_flags(settings_cls: type[BaseSettings] = Settings) -> list[str]:
    """The development-only fields of ``settings_cls``: every field declared with
    ``dev_only_flag`` plus every boolean field whose name matches ``DEV_ONLY_NAME``."""
    names = []
    for name, field in settings_cls.model_fields.items():
        marked = _is_marked_dev_only(field.json_schema_extra)
        looks_dev_only = field.annotation is bool and DEV_ONLY_NAME.search(name) is not None
        if marked or looks_dev_only:
            names.append(name)
    return sorted(names)


def enabled_dev_only_flags(settings: Settings, flags: Iterable[str] | None = None) -> list[str]:
    """Names of the development-only flags that are switched on in ``settings``."""
    names = dev_only_flags(type(settings)) if flags is None else flags
    return [name for name in names if bool(getattr(settings, name, False))]


def check_dev_only_flags(settings: Settings, flags: Iterable[str] | None = None) -> None:
    """Start-up guard: refuse any development-only flag while ``ENV=prod``.

    Called from the API lifespan, so a misconfigured production container crashes at start
    instead of serving a development-only feature.
    """
    if settings.env != "prod":
        return
    enabled = enabled_dev_only_flags(settings, flags)
    if enabled:
        raise DevOnlyFlagInProdError(
            "ENV=prod but development-only flags are enabled: "
            + ", ".join(sorted(enabled))
            + " — disable them, or set ENV=dev on a local machine"
        )
