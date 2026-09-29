"""db/roles.sql (T11.0.13): idempotent roles, default privileges for secrag_backup;
scripts/db/apply_roles.sh sends passwords only as SCRAM verifiers (DA-C-3)."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, text

from db_harness import ROLES_SQL, apply_roles

pytestmark = pytest.mark.db

APPLY_ROLES = Path(__file__).resolve().parents[2] / "scripts" / "db" / "apply_roles.sh"


def test_apply_roles_sends_a_scram_verifier_and_the_password_logs_in(
    db_url: URL, tmp_path: Path
) -> None:
    """Real psql against the harness database, wrapped with --echo-queries: psql prints each
    query exactly as sent to the server (after variable interpolation). The password must
    not be in it — only a SCRAM verifier — and the role must then log in with the password."""
    real_psql = shutil.which("psql")
    assert real_psql, "psql is required for this test (CI and the gate have it)"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "psql"
    wrapper.write_text(f'#!/bin/sh\nexec "{real_psql}" --echo-queries "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)

    password = "t-" + secrets.token_hex(12)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"SECRAG_PURGER_PASSWORD", "SECRAG_BACKUP_PASSWORD", "PGPASSWORD"}
    }
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "SECRAG_PURGER_PASSWORD": password}
    proc = subprocess.run(
        ["bash", str(APPLY_ROLES), db_url.render_as_string(hide_password=False)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    sent = proc.stdout + proc.stderr
    assert "ALTER ROLE secrag_purger LOGIN PASSWORD 'SCRAM-SHA-256$4096:" in sent
    assert password not in sent
    assert "secrag_backup stays NOLOGIN" in sent

    role_url = db_url.set(username="secrag_purger", password=password)
    engine = create_engine(role_url, future=True)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar_one() == "secrag_purger"
    finally:
        engine.dispose()
    wrong = create_engine(db_url.set(username="secrag_purger", password="wrong"), future=True)
    try:
        with pytest.raises(Exception, match="password authentication failed"):
            wrong.connect().close()
    finally:
        wrong.dispose()


SNAPSHOT = """
SELECT 'role:' || rolname || ':' || rolcanlogin || ':' || rolsuper || ':' || rolcreaterole
  FROM pg_roles WHERE rolname IN ('secrag_purger', 'secrag_backup')
UNION ALL SELECT 'db:' || coalesce(datacl::text, '')
  FROM pg_database WHERE datname = current_database()
UNION ALL SELECT 'schema:' || coalesce(nspacl::text, '') FROM pg_namespace WHERE nspname = 'public'
UNION ALL SELECT 'default:' || defaclobjtype::text || ':' || coalesce(defaclacl::text, '')
  FROM pg_default_acl
UNION ALL SELECT 'rel:' || relname || ':' || coalesce(relacl::text, '')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S')
ORDER BY 1
"""


def _snapshot(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(text(SNAPSHOT))]


def test_roles_sql_is_plain_sql_without_psql_meta_commands() -> None:
    # The harness and CI execute it through a driver, not only through psql.
    lines = ROLES_SQL.read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if line.lstrip().startswith("\\")]


def test_running_roles_sql_twice_changes_nothing(db_engine: Engine) -> None:
    before = _snapshot(db_engine)  # the harness already applied it once
    assert apply_roles(db_engine)
    assert _snapshot(db_engine) == before
    assert any(s.startswith("role:secrag_purger:") for s in before)
    assert any(s.startswith("role:secrag_backup:") for s in before)


def test_backup_role_reads_tables_created_after_roles_sql(db_engine: Engine) -> None:
    """W16: tables the owner creates later (migrations) stay dumpable without a new grant."""
    with db_engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ]
        assert "chunks" in tables and "users" in tables
        for table in tables:
            assert conn.execute(
                text("SELECT has_table_privilege('secrag_backup', :t, 'SELECT')"),
                {"t": f"public.{table}"},
            ).scalar_one(), table
        conn.execute(text("CREATE TABLE roles_probe (id int)"))
        assert conn.execute(
            text("SELECT has_table_privilege('secrag_backup', 'public.roles_probe', 'SELECT')")
        ).scalar_one()
        conn.rollback()


def test_roles_are_not_superusers_and_have_no_write_rights_yet(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
                "WHERE rolname IN ('secrag_purger', 'secrag_backup')"
            )
        ).all()
        assert len(rows) == 2
        assert all(not (r.rolsuper or r.rolcreaterole or r.rolcreatedb) for r in rows)
        assert not conn.execute(
            text("SELECT has_table_privilege('secrag_backup', 'public.users', 'DELETE')")
        ).scalar_one()
        # The purger's table rights come with migration 0005 (row 20), not with roles.sql.
        assert not conn.execute(
            text("SELECT has_table_privilege('secrag_purger', 'public.users', 'DELETE')")
        ).scalar_one()
