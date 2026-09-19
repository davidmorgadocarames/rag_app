"""Typed application configuration.

All configuration is loaded from environment variables (or a local `.env` file).
Nothing sensitive is hardcoded; see `.env.example` for the full list of keys.
"""

from __future__ import annotations

from pydantic import Field
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

    # --- Ollama / models (only 3 models across the whole system) ---
    ollama_host: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    embed_model: str = "bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

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

    # --- Auth / security ---
    jwt_secret: str = ""
    jwt_expires_minutes: int = 30

    # --- Email verification (SMTP) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@example.com"

    # --- Frontend ---
    next_public_api_url: str = Field(default="http://localhost:8000")


def get_settings() -> Settings:
    """Return the application settings loaded from the environment."""
    return Settings()
