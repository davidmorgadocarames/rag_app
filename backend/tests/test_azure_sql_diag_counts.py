"""``scripts/azure/sql/diag_counts.sql`` (DEBT-031 / DEC-009 = B): the script must keep
running cleanly with ``ON_ERROR_STOP`` on BOTH the live, not-yet-promoted Azure database
(migration 0005, no ``sessions`` table) and a database already migrated to 0006 -- a bare
``SELECT ... FROM sessions`` would abort the whole script on 0005 (Postgres resolves the
table reference at parse time, before any ``CASE``/``to_regclass`` guard inside the same
statement could matter), so the fix uses psql's own ``\\gset``/``\\if`` scripting instead.

Each test migrates a FRESH throwaway database to a specific revision (never the shared
session-scoped harness database other tests rely on) and drops it afterwards.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from db_harness import apply_roles, create_database, drop_database

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
SCRIPT = REPO_ROOT / "scripts" / "azure" / "sql" / "diag_counts.sql"


def _migrate_to(url: URL, revision: str) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    rendered = url.render_as_string(hide_password=False).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", rendered)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, revision)


def _run_script(url: URL) -> dict[str, str]:
    plain = url.set(drivername="postgresql", password=None)
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    result = subprocess.run(
        ["psql", "-X", "-At", "-f", str(SCRIPT), plain.render_as_string(hide_password=False)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("|", 1) for line in result.stdout.splitlines() if line)


@pytest.fixture()
def throwaway_db(admin_url: URL):
    url = create_database(admin_url)
    engine = create_engine(url, future=True)
    try:
        apply_roles(engine)
    finally:
        engine.dispose()
    try:
        yield url
    finally:
        drop_database(admin_url, url)


def test_the_script_runs_cleanly_before_0006_adds_sessions(throwaway_db: URL) -> None:
    _migrate_to(throwaway_db, "0005_persistence_erasure")
    lines = _run_script(throwaway_db)
    assert "not present" in lines["sessions"]
    assert lines["users"] == "0"  # the rest of the script still works unguarded


def test_the_script_counts_sessions_once_0006_is_applied(throwaway_db: URL) -> None:
    _migrate_to(throwaway_db, "head")
    lines = _run_script(throwaway_db)
    assert lines["sessions"] == "0"
