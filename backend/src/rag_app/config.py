"""Typed application configuration.

All configuration is loaded from environment variables (or a local `.env` file).
Nothing sensitive is hardcoded; see `.env.example` for the full list of keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only features (PHASE_PLANNING §1 rule 2, "fail-closed flags"): fake LLM
# provider (Phase 16), defence lab (13), data explorer (19), code-fix (20). Each one is a
# boolean Settings field declared with `dev_only_flag(...)`; the registry is DERIVED from that
# field metadata (DA-C-5), so there is no separate list to forget. With ENV=prod (the
# default) the API refuses to start while any of them is on. As a backstop, a boolean field
# (plain `bool`, `bool | None`, `Optional[bool]` or `StrictBool`)
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


class SettingsValidationError(RuntimeError):
    """The API refuses to start: a required setting is missing or invalid (T11.2.2).

    The message names the settings and the rule, never a value."""


# Development default of DATABASE_URL (the local compose database). With ENV=prod the API
# refuses to start on it: production must say where its database is.
DEV_DATABASE_URL = "postgresql+psycopg://rag:rag@localhost:5432/rag"
JWT_SECRET_MIN_LENGTH = 32


class JobSettings(BaseSettings):
    """The minimal settings of the Jobs (migrations, purge, backup — T11.2.2).

    A Job only needs to know where the database is. It never reads, needs or validates
    ``JWT_SECRET`` or ``DATA_MASTER_KEY``, so the Jobs' Container Apps definitions do not
    carry those secrets, and a Job starts without them.
    """

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
    database_url: str = DEV_DATABASE_URL

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


class Settings(JobSettings):
    """Application (API) settings, populated from the environment / `.env`.

    Constructing them never fails on a missing secret; the API validates them in its
    lifespan (``validate_api_settings``) and refuses to start. Jobs use ``JobSettings``.
    """

    # --- Ollama / models (only 3 models across the whole system) ---
    ollama_host: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    embed_model: str = "bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # Hugging Face commit of reranker_model (a full 40-hex commit hash, never a branch/tag):
    # the loader reads exactly this snapshot — from the local cache first, offline — so a
    # changed or compromised Hub repo cannot change the code/config that runs (DA-B-7).
    reranker_revision: str = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
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
    # Output bound of the answer generation call (DA-31b-3; the worst-case cost per answer in
    # ADR 11 "Costs" uses it). The groundedness check has its own bound (generation.py).
    max_tokens: int = 1024

    # --- Ollama context window per call type (T11.3.4) ---
    # Explicit so a request is never silently truncated by Ollama's small built-in default
    # (2048): worst case today is rerank_top_n (4) chunks of up to chunk_size + chunk_overlap
    # (~1350 chars each) plus the system prompt, the question and the output budget
    # (max_tokens=1024 for the answer, GROUNDEDNESS_MAX_TOKENS=16 for groundedness) comes
    # close to or over 2048 tokens already. Measured on the development machine (RTX 4060,
    # qwen2.5:7b-instruct-q4_K_M + bge-m3 both resident): num_ctx 2048/4096/8192 cost
    # ~4.53/4.64/4.87 GiB VRAM for qwen alone (bge-m3 adds ~0.74 GiB) — comfortably under the
    # 8 GiB card even at 8192 (~1.8 GiB headroom); see ADR 11 decision 8 for the full table.
    # **Both values below MUST stay equal.** Ollama reloads the whole model (~6.3 s measured)
    # whenever a request's `num_ctx` differs from the currently loaded one — including a
    # request that omits `num_ctx` entirely (it then means Ollama's own default, 2048, which
    # already differs from either value here). Since one answer always calls generate then
    # groundedness back to back, a mismatch would reload qwen *twice* per answer (once for
    # groundedness, once more for the next answer's generate) — a measured ~12.6 s regression
    # that the quality/functional tests cannot see (they do not pin down wall-clock time).
    # `test_num_ctx_answer_and_groundedness_must_match` (test_llm_bounds.py) guards this.
    # Azure OpenAI's context window is fixed by the deployment, not by this setting — these
    # two are read only on the Ollama path (`LLM_PROVIDER=ollama`; see llm.py). DA-11bB-1
    # (block C, 11b): the agentic query rewrite (`agentic.reformulate`) and the eval
    # correctness judge (`eval.judge.judge_correctness`) are NOT a future risk — the `eval`
    # gate step (`eval.benchmark`/`eval.runner`) already reuses ONE `OllamaChat` across
    # generate -> groundedness -> judge (and the router's rewrite, when exercised) on every
    # golden-set item, i.e. the SAME process/keep-alive window as generate/groundedness,
    # today. Both now pin `num_ctx_answer` too (`test_router_and_judge_calls_now_pin_num_ctx_
    # to_match_answer_groundedness`, test_llm_bounds.py) so qwen is never reloaded mid-run. A
    # future conversation-summary call must do the same the moment it can share a process/
    # keep-alive window with any of these — never assume "CLI/eval only" means "safe to omit".
    num_ctx_answer: int = 8192
    num_ctx_groundedness: int = 8192

    # --- Agentic router ---
    # Best rerank (cross-encoder) score below this is considered "thin" -> reformulate+retry.
    thin_threshold: float = 0.5
    max_query_rewrites: int = 1

    # --- Rate limiting (token bucket) ---
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0
    rate_limit_chat_cost: float = 5.0  # cost-aware: an LLM answer costs more than 1 token

    # --- Global daily answer cap (R6-1, rag_app.usage_cap) ---
    # Answers per UTC day for ALL users together, counted in `usage_daily` before any LLM
    # call. 0 = off (ENV=dev only; ENV=prod requires a positive cap). Azure runs 150
    # (D-2026-10-01-1) = this default, so a lost env var (gotcha 7) can never raise it.
    daily_answer_cap: int = 150

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

    # --- Metrics (T11.3.2, TF4) ---
    # Prometheus histograms + the in-flight gauge are served on this SEPARATE internal
    # port — never the API port (`/metrics` would expose route/volume/error shape to the
    # browser). Only Prometheus (compose network / Azure, never published to a host port
    # or the internet) reads it.
    metrics_port: int = 9100


def get_settings() -> Settings:
    """Return the application settings loaded from the environment."""
    return Settings()


def get_job_settings() -> JobSettings:
    """Return the Jobs' minimal settings (no secrets besides the database URL)."""
    return JobSettings()


def _database_url_problem(settings: JobSettings) -> str | None:
    from sqlalchemy.engine import make_url

    raw = settings.database_url.strip()
    if not raw:
        return "DATABASE_URL is empty"
    try:
        url = make_url(raw)
    except Exception:  # noqa: BLE001 - the message could echo the URL (password)
        return "DATABASE_URL is not a database URL"
    if not url.drivername.startswith("postgresql"):
        return "DATABASE_URL must be a PostgreSQL URL"
    if not url.database:
        return "DATABASE_URL names no database"
    if settings.env == "prod" and "database_url" not in settings.model_fields_set:
        return "DATABASE_URL is not set (ENV=prod refuses the local development default)"
    return None


def validate_api_settings(settings: Settings) -> None:
    """Fail-fast validation of the API's settings (T11.2.2), called from the API lifespan
    only — never at import time and never by a Job.

    Checks ``DATABASE_URL`` (a PostgreSQL URL; explicitly set when ``ENV=prod``),
    ``JWT_SECRET`` (at least 32 characters), ``DATA_MASTER_KEY`` (a valid Fernet key) and
    ``ENV`` (``dev``/``prod``) and ``DAILY_ANSWER_CAP`` (never negative; positive with
    ``ENV=prod``). Every problem is reported at once, by setting name only.
    """
    from cryptography.fernet import Fernet

    problems: list[str] = []
    if settings.env not in ("dev", "prod"):  # also enforced by the Literal type
        problems.append("ENV must be 'dev' or 'prod'")
    db_problem = _database_url_problem(settings)
    if db_problem:
        problems.append(db_problem)
    if len(settings.jwt_secret) < JWT_SECRET_MIN_LENGTH:
        problems.append(f"JWT_SECRET must be at least {JWT_SECRET_MIN_LENGTH} characters")
    try:
        Fernet(settings.data_master_key.encode())
    except Exception:  # noqa: BLE001 - never echo the key
        problems.append(
            "DATA_MASTER_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)"
        )
    if settings.daily_answer_cap < 0:
        problems.append("DAILY_ANSWER_CAP must be 0 (off, ENV=dev only) or a positive number")
    elif settings.env == "prod" and settings.daily_answer_cap == 0:
        problems.append("DAILY_ANSWER_CAP must be positive with ENV=prod (0 = off is dev only)")
    if problems:
        raise SettingsValidationError(
            "refusing to start — invalid settings: " + "; ".join(problems)
        )


def _is_marked_dev_only(extra: object) -> bool:
    return isinstance(extra, dict) and extra.get(DEV_ONLY_MARK) is True


def _is_boolish(annotation: object) -> bool:
    """``bool`` and its wrappers: ``bool | None``, ``Optional[bool]``, ``StrictBool`` /
    ``Annotated[bool, …]`` (DA-C2-4) — any of them can switch a feature on."""
    if annotation is bool:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _is_boolish(get_args(annotation)[0])
    if origin in (Union, UnionType):
        return any(_is_boolish(a) for a in get_args(annotation) if a is not type(None))
    return False


def dev_only_flags(settings_cls: type[BaseSettings] = Settings) -> list[str]:
    """The development-only fields of ``settings_cls``: every field declared with
    ``dev_only_flag`` plus every boolean field whose name matches ``DEV_ONLY_NAME``."""
    names = []
    for name, field in settings_cls.model_fields.items():
        marked = _is_marked_dev_only(field.json_schema_extra)
        looks_dev_only = _is_boolish(field.annotation) and DEV_ONLY_NAME.search(name) is not None
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
