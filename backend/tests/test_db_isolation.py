"""App code under a DB test only ever reaches the harness database (DA-B-3), and Alembic
keeps taking its URL from ``DATABASE_URL`` (DA-B-12)."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from db_harness import BACKEND_DIR, DB_PREFIX, HarnessRefusal, check_app_url

DEV_URL = "postgresql+psycopg://rag:rag@localhost:5432/rag"


# --- unit (no database) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        DEV_URL,  # the development database
        "postgresql+psycopg://secrag_gate:x@127.0.0.1:15432/secrag_gate",  # gate DB, not harness
        "postgresql+psycopg://u:p@db.example.com:15432/secrag_test_abc",  # remote host
        "postgresql+psycopg://u:p@localhost/secrag_test_abc",  # default port = 5432
    ],
)
def test_app_urls_other_than_a_harness_database_are_refused(url: str) -> None:
    with pytest.raises(HarnessRefusal):
        check_app_url(url)


def test_a_harness_database_url_is_accepted() -> None:
    check_app_url("postgresql+psycopg://u:p@127.0.0.1:15432/secrag_test_0123456789ab")


def test_alembic_ini_keeps_sqlalchemy_url_empty() -> None:
    """migrations/env.py uses DATABASE_URL only when alembic.ini's sqlalchemy.url is empty;
    a value there would make CI, the gate and the migrations Job ignore DATABASE_URL."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(BACKEND_DIR / "alembic.ini", encoding="utf-8")
    assert parser.has_option("alembic", "sqlalchemy.url")
    assert parser.get("alembic", "sqlalchemy.url").strip() == ""


# --- DB tests (harness) -----------------------------------------------------------------


@pytest.mark.db
def test_app_code_reaches_only_the_harness_database() -> None:
    from rag_app.api import deps
    from rag_app.config import get_settings
    from rag_app.db.session import make_engine

    url = make_url(get_settings().database_url)
    assert (url.database or "").startswith(DB_PREFIX)
    assert url.port != 5432 and url.database != "rag"

    engine = make_engine()
    try:
        with engine.connect() as conn:
            name = conn.execute(text("SELECT current_database()")).scalar_one()
    finally:
        engine.dispose()
    assert name == url.database

    session_gen = deps.get_session()  # what every API endpoint gets injected
    session = next(session_gen)
    try:
        assert session.execute(text("SELECT current_database()")).scalar_one() == name
    finally:
        session_gen.close()


@pytest.mark.db
def test_a_dev_database_url_in_the_environment_is_overridden() -> None:
    """Run a DB test in a fresh pytest with DATABASE_URL = the development database (as a
    shell with backend/.env would have): app code must still get the harness database."""
    env = {**os.environ, "DATABASE_URL": DEV_URL, "SECRAG_REQUIRE_DB_TESTS": "1"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            f"{BACKEND_DIR / 'tests' / 'test_db_isolation.py'}::"
            "test_app_code_reaches_only_the_harness_database",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout
