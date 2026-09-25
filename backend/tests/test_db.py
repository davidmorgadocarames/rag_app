"""The DB URL must resolve to the psycopg v3 driver, never psycopg2.

Guards the "container runs its own migrations" path: a bare cloud Postgres URL
(no ``+driver``) must not fall back to the unshipped psycopg2 dialect, which
crash-loops the backend on ``alembic upgrade head``. No live database required —
the engine is built but never connects.
"""

from __future__ import annotations

import pytest

from rag_app.config import Settings
from rag_app.db.session import make_engine


@pytest.mark.parametrize(
    "raw",
    ["postgresql://u:p@host:5432/db", "postgres://u:p@host:5432/db"],
)
def test_bare_postgres_url_normalized_to_psycopg3(raw: str) -> None:
    assert Settings(database_url=raw).database_url == "postgresql+psycopg://u:p@host:5432/db"


def test_query_string_is_preserved() -> None:
    raw = "postgresql://u:p@host:5432/db?sslmode=require"
    assert Settings(database_url=raw).database_url == (
        "postgresql+psycopg://u:p@host:5432/db?sslmode=require"
    )


def test_explicit_driver_is_left_untouched() -> None:
    url = "postgresql+asyncpg://u:p@host/db"
    assert Settings(database_url=url).database_url == url


def test_engine_from_bare_url_uses_psycopg3_not_psycopg2() -> None:
    # Regression guard: this raises ModuleNotFoundError('psycopg2') if the driver
    # normalization is ever removed — exactly the Azure crash-loop we hit.
    settings = Settings(database_url="postgresql://u:p@host:5432/db")
    engine = make_engine(settings.database_url)
    assert engine.dialect.driver == "psycopg"
