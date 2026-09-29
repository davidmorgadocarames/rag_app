"""db/roles.sql (T11.0.13): idempotent roles, default privileges for secrag_backup."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from db_harness import ROLES_SQL, apply_roles

pytestmark = pytest.mark.db

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
