"""Alembic migration environment."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the application package importable (backend/src).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_app.config import get_job_settings  # noqa: E402
from rag_app.db.models import Base  # noqa: E402

config = context.config
# A URL set programmatically (the test DB harness) wins; otherwise the Jobs' minimal settings
# (only DATABASE_URL: the migration Job runs without JWT_SECRET / DATA_MASTER_KEY, T11.2.2).
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_job_settings().database_url.replace("%", "%%"))

# Callers embedding Alembic (the test harness) keep their own logging configuration.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
