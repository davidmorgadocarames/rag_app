"""Migration 0005 (T11.2.3) and the master-key fingerprint at start-up (T11.2.4), on
harness databases (marker ``db``: gate --full / CI)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from db_harness import BACKEND_DIR, apply_roles, create_database, drop_database
from rag_app.api.app import create_app
from rag_app.config import Settings
from rag_app.keycheck import (
    MasterKeyMismatchError,
    UnreadableKeysError,
    check_master_key_fingerprint,
    fingerprint,
)

pytestmark = pytest.mark.db

MIGRATION_0005 = BACKEND_DIR / "migrations" / "versions" / "0005_persistence_erasure.py"


def _alembic(url: URL, script_location: Path | None = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(script_location or BACKEND_DIR / "migrations"))
    cfg.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture()
def fresh_db(admin_url: URL) -> Iterator[URL]:
    """A new harness database with the roles applied but NOT migrated."""
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


def _engine(url: URL) -> Engine:
    return create_engine(url, future=True)


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _seed_user(engine: Engine, *, erased: bool = False, master: str | None = None) -> uuid.UUID:
    user_id = uuid.uuid4()
    wrapped = Fernet((master or Fernet.generate_key().decode()).encode()).encrypt(b"user-key")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'h')"),
            {"id": user_id, "e": f"{user_id.hex}@example.test"},
        )
        conn.execute(
            text("INSERT INTO user_keys (user_id, wrapped_key) VALUES (:id, :w)"),
            {"id": user_id, "w": wrapped},
        )
        conn.execute(
            text("INSERT INTO conversations (id, user_id) VALUES (:c, :id)"),
            {"c": uuid.uuid4(), "id": user_id},
        )
        conn.execute(
            text("INSERT INTO deletion_requests (id, user_id) VALUES (:d, :u)"),
            {"d": uuid.uuid4(), "u": uuid.uuid4()},  # an old Phase-6 tombstone
        )
        if erased:
            conn.execute(
                text("UPDATE users SET deleted_at = now(), email = NULL WHERE id = :id"),
                {"id": user_id},
            )
    return user_id


# --- 0005 -------------------------------------------------------------------------------


def test_0005_round_trip_on_an_empty_database(fresh_db: URL) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    engine = _engine(fresh_db)
    try:
        assert _version(engine) == "0005_persistence_erasure"
        command.downgrade(cfg, "-1")
        assert _version(engine) == "0004_conv_titles_tokens"
        command.upgrade(cfg, "head")
        assert _version(engine) == "0005_persistence_erasure"
    finally:
        engine.dispose()


def test_0005_round_trip_on_a_seeded_database_without_erased_users(fresh_db: URL) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "0004_conv_titles_tokens")
    engine = _engine(fresh_db)
    try:
        user_id = _seed_user(engine)
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            status, completed = conn.execute(
                text("SELECT status, completed_at IS NOT NULL FROM deletion_requests")
            ).one()
            assert (status, completed) == ("done", True)  # old tombstones are complete
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM users WHERE id = :id AND email IS NOT NULL"),
                    {"id": user_id},
                ).scalar_one()
                == 1
            )
            assert conn.execute(text("SELECT count(*) FROM user_keys")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
            # new tombstones default to pending (the purger's queue, 11.2b)
            conn.execute(
                text("INSERT INTO deletion_requests (id, user_id) VALUES (:d, :u)"),
                {"d": uuid.uuid4(), "u": uuid.uuid4()},
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM deletion_requests WHERE status = 'pending'")
                ).scalar_one()
                == 1
            )
            conn.rollback()
    finally:
        engine.dispose()


def test_0005_downgrade_refuses_while_erased_users_exist(fresh_db: URL) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    engine = _engine(fresh_db)
    try:
        _seed_user(engine, erased=True)
        with pytest.raises(RuntimeError, match="refusing to downgrade 0005"):
            command.downgrade(cfg, "-1")
        assert _version(engine) == "0005_persistence_erasure"  # nothing was changed
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0005_grants_follow_the_migration_map(db_engine: Engine) -> None:
    def can(role: str, table: str, privilege: str) -> bool:
        with db_engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": f"public.{table}", "p": privilege},
                ).scalar_one()
            )

    def can_column(role: str, table: str, column: str, privilege: str) -> bool:
        with db_engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT has_column_privilege(:r, :t, :c, :p)"),
                    {"r": role, "t": f"public.{table}", "c": column, "p": privilege},
                ).scalar_one()
            )

    p = "secrag_purger"
    for table in ("messages", "conversations", "email_verification_tokens"):
        assert can(p, table, "SELECT") and can(p, table, "DELETE"), table
        assert can_column(p, table, "id", "UPDATE"), table
        assert not can(p, table, "INSERT"), table
    assert not can_column(p, "messages", "content_encrypted", "UPDATE")
    assert all(can(p, "users", x) for x in ("SELECT", "UPDATE", "DELETE"))
    assert not can(p, "users", "INSERT")
    assert can(p, "deletion_requests", "UPDATE") and not can(p, "deletion_requests", "DELETE")
    assert can(p, "user_keys", "SELECT") and not can(p, "user_keys", "DELETE")
    assert can(p, "purger_runs", "INSERT") and can(p, "purger_runs", "UPDATE")
    for table in ("chunks", "documents", "master_key_fingerprint", "usage_daily"):
        assert not can(p, table, "SELECT"), table
    for table in ("master_key_fingerprint", "purger_runs", "usage_daily", "users"):
        assert can("secrag_backup", table, "SELECT"), table
        assert not can("secrag_backup", table, "UPDATE"), table


def _load_0005() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0005", MIGRATION_0005)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0005_fails_clearly_when_a_role_is_missing(db_engine: Engine) -> None:
    module = _load_0005()
    module.REQUIRED_ROLES = ("secrag_purger", "secrag_role_that_does_not_exist")
    with db_engine.connect() as conn, Operations.context(MigrationContext.configure(conn)):
        with pytest.raises(RuntimeError, match="secrag_role_that_does_not_exist"):
            module._require_roles()


def test_a_broken_downgrade_fails_the_round_trip(fresh_db: URL, tmp_path: Path) -> None:
    """Row 27: `alembic downgrade -1` (what `migrations-roundtrip` runs) exits non-zero when a
    migration's downgrade is broken."""
    scripts = tmp_path / "migrations"
    shutil.copytree(
        BACKEND_DIR / "migrations", scripts, ignore=shutil.ignore_patterns("__pycache__")
    )
    (scripts / "versions" / "0099_broken_downgrade.py").write_text(
        '"""broken downgrade (test only)"""\n'
        "from alembic import op\n"
        'revision = "0099_broken_downgrade"\n'
        'down_revision = "0005_persistence_erasure"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade():\n"
        '    op.execute("CREATE TABLE broken_probe (id int)")\n\n\n'
        "def downgrade():\n"
        '    op.execute("DROP TABLE table_that_does_not_exist")\n',
        encoding="utf-8",
    )
    ini = tmp_path / "alembic.ini"
    ini.write_text(
        (BACKEND_DIR / "alembic.ini")
        .read_text(encoding="utf-8")
        .replace("script_location = migrations", f"script_location = {scripts}"),
        encoding="utf-8",
    )
    env = dict(os.environ, DATABASE_URL=fresh_db.render_as_string(hide_password=False))

    def alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ini), *args],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    down = alembic("downgrade", "-1")
    assert down.returncode != 0
    assert "table_that_does_not_exist" in down.stderr


# --- fingerprint --------------------------------------------------------------------------


@pytest.fixture()
def migrated(fresh_db: URL) -> Iterator[Engine]:
    command.upgrade(_alembic(fresh_db), "head")
    engine = _engine(fresh_db)
    try:
        yield engine
    finally:
        engine.dispose()


def _stored(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [
            str(r[0]) for r in conn.execute(text("SELECT fingerprint FROM master_key_fingerprint"))
        ]


def test_first_start_stores_the_fingerprint_and_the_same_key_starts(migrated: Engine) -> None:
    key = Fernet.generate_key().decode()
    _seed_user(migrated, master=key)
    assert check_master_key_fingerprint(migrated, key) == "stored"
    assert _stored(migrated) == [fingerprint(key)]
    assert check_master_key_fingerprint(migrated, key) == "match"
    assert _stored(migrated) == [fingerprint(key)]


def test_a_different_key_aborts(migrated: Engine) -> None:
    key, other = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    check_master_key_fingerprint(migrated, key)
    with pytest.raises(MasterKeyMismatchError) as info:
        check_master_key_fingerprint(migrated, other)
    assert key not in str(info.value) and other not in str(info.value)
    assert fingerprint(key) not in str(info.value)
    assert _stored(migrated) == [fingerprint(key)]  # never overwritten


def test_unreadable_keys_block_the_first_fingerprint_write(migrated: Engine) -> None:
    """R5-5: with no fingerprint yet, a key that cannot unwrap every stored user key refuses
    to start and stores nothing."""
    key = Fernet.generate_key().decode()
    _seed_user(migrated, master=key)
    _seed_user(migrated, master=Fernet.generate_key().decode())  # wrapped by an old key
    with pytest.raises(UnreadableKeysError, match="1 of 2 stored user keys"):
        check_master_key_fingerprint(migrated, key)
    assert _stored(migrated) == []


def test_api_lifespan_end_to_end(migrated: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """`with TestClient(app)`: first start stores, same key restarts, another key aborts."""
    url = migrated.url.render_as_string(hide_password=False)
    key, other = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    def settings(master: str) -> Settings:
        return Settings(
            _env_file=None, database_url=url, jwt_secret="j" * 40, data_master_key=master
        )

    # T11.4.1: the lifespan now also warms up the real (heavy, downloaded) cross-encoder —
    # this DB test is about the master-key fingerprint, not the reranker, so stub it.
    class _NoWarmupReranker:
        def warm_up(self) -> None:
            return None

    monkeypatch.setattr(
        "rag_app.api.app.reranking.get_shared_reranker", lambda: _NoWarmupReranker()
    )
    monkeypatch.setattr("rag_app.api.app.get_settings", lambda: settings(key))
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
    assert _stored(migrated) == [fingerprint(key)]
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
    monkeypatch.setattr("rag_app.api.app.get_settings", lambda: settings(other))
    with pytest.raises(MasterKeyMismatchError), TestClient(create_app()):
        pass
