"""Typed application configuration.

All configuration is loaded from environment variables (or a local `.env` file).
Nothing sensitive is hardcoded; see `.env.example` for the full list of keys.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, populated from the environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
    # The Azure_* keys are only read when llm_provider == "azure_openai"; see ADR 0004.
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
