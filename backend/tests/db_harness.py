"""Test DB harness (T11.0.14): DB tests only ever run on a database the harness created.

``TEST_DATABASE_URL`` names a *maintenance* connection on a local test server (the gate
project's Postgres on 127.0.0.1:15432 by default, or the CI service). The harness:

1. **refuses** the URL before connecting when it could be the development database —
   port 5432 (explicit or default), database ``rag``, or any host that is not loopback
   (so never an Azure server);
2. creates a fresh database ``secrag_test_<random>`` and marks it with a comment;
3. refuses to hand out any database that does not carry that marker;
4. migrates it (``alembic upgrade head``), applies ``db/roles.sql`` when that file exists,
   and drops it at the end of the session.

5. points the **app's own configuration** at that database for every ``db`` test:
   ``DATABASE_URL`` (the only setting the app builds its engine from) is set to the harness
   database for the session, the app's cached session factories are reset around each test,
   and a test aborts the run (rc 4) if app code would still resolve anything else — so a DB
   test calling ``get_settings()``, ``make_engine()`` or the API can never reach the
   development database on 5432 / ``rag`` (DA-B-3).

Selection (see ``conftest.py``): tests marked ``db`` are skipped when ``TEST_DATABASE_URL``
is unset (``gate.sh --fast``), and that skip becomes an error when
``SECRAG_REQUIRE_DB_TESTS=1`` (``gate.sh --full`` and CI).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
ROLES_SQL = REPO_ROOT / "db" / "roles.sql"

MARKER = "created-by-secrag-test-harness"
DB_PREFIX = "secrag_test_"
DEV_PORT = 5432
DEV_DATABASES = frozenset({"rag"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class HarnessRefusal(RuntimeError):
    """The URL or database is not one the harness may use."""


def check_admin_url(raw: str) -> URL:
    """Validate ``TEST_DATABASE_URL`` without connecting; raise ``HarnessRefusal``."""
    try:
        url = make_url(raw)
    except Exception as exc:  # noqa: BLE001 - any parse error is a refusal
        raise HarnessRefusal(f"TEST_DATABASE_URL is not a database URL ({exc})") from exc
    if not url.drivername.startswith("postgresql"):
        raise HarnessRefusal(f"TEST_DATABASE_URL must be PostgreSQL, got {url.drivername!r}")
    if (url.host or "localhost") not in LOOPBACK_HOSTS:
        raise HarnessRefusal(f"refusing non-local host {url.host!r}: tests run on a local server")
    if (url.port or DEV_PORT) == DEV_PORT:
        raise HarnessRefusal(
            "refusing port 5432: that is the development database; use the gate project"
            " (127.0.0.1:15432) or the CI service"
        )
    if (url.database or "") in DEV_DATABASES:
        raise HarnessRefusal(f"refusing database {url.database!r}: the development database")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url


def check_app_url(raw: str) -> URL:
    """The URL app code resolves (``DATABASE_URL`` via settings) during a DB test must be a
    harness database: the admin-URL rules plus the ``secrag_test_`` name (DA-B-3)."""
    url = check_admin_url(raw)
    if not (url.database or "").startswith(DB_PREFIX):
        raise HarnessRefusal(f"app code would use database {url.database!r}, not a harness one")
    return url


def assert_harness_database(engine: Engine) -> None:
    """Refuse any database the harness did not create (name prefix + comment marker)."""
    with engine.connect() as conn:
        name, comment = conn.execute(
            text(
                "SELECT current_database(), shobj_description(d.oid, 'pg_database')"
                " FROM pg_database d WHERE d.datname = current_database()"
            )
        ).one()
    if not str(name).startswith(DB_PREFIX) or comment != MARKER:
        raise HarnessRefusal(f"refusing database {name!r}: not created by the test DB harness")


def _admin_engine(admin_url: URL) -> Engine:
    return create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)


def create_database(admin_url: URL) -> URL:
    """Create and mark a fresh test database; return its URL."""
    name = f"{DB_PREFIX}{secrets.token_hex(6)}"
    engine = _admin_engine(admin_url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            conn.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{MARKER}'"))
    finally:
        engine.dispose()
    return admin_url.set(database=name)


def drop_database(admin_url: URL, test_url: URL) -> None:
    name = test_url.database or ""
    if not name.startswith(DB_PREFIX):  # never drop anything else
        raise HarnessRefusal(f"refusing to drop {name!r}")
    engine = _admin_engine(admin_url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def migrate(test_url: URL) -> None:
    """``alembic upgrade head`` on the test database (the app's own migrations)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    rendered = test_url.render_as_string(hide_password=False).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", rendered)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


def apply_roles(engine: Engine) -> bool:
    """Apply ``db/roles.sql`` when it exists (row 12 adds it); True when applied."""
    if not ROLES_SQL.is_file():
        return False
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(ROLES_SQL.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    return True
