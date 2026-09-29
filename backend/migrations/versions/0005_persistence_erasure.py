"""persistence + asynchronous erasure (expand): master-key fingerprint, erasure columns,
tombstone bookkeeping, purger runs, daily usage; per-role grants

Revision ID: 0005_persistence_erasure
Revises: 0004_conv_titles_tokens
Create Date: 2026-09-30

Expand-only (docs/DEFINITION_OF_DONE.md, "Expand/contract"): every change is additive or
relaxes a constraint, so the previous image keeps working on the migrated schema.

Authored once for 11a (TF7): 11.2 (block E) writes it and 11.2b (block G) EXTENDS THIS FILE
— no 0006 — before it is applied to any shared database (Azure or the developer's DB).
Extension points are the data tables below (``NEW_TABLES``, ``PURGER_GRANTS``,
``BACKUP_TABLES``): add a table/column with its grant there and in ``downgrade`` and keep
``migrations-roundtrip`` green.

Grants follow PHASE_PLANNING's migration map (X4). The roles come from ``db/roles.sql``,
which runs before every migrate; a missing role fails the upgrade with a clear message. The
app still runs as the database owner, so the map's "app role" grant on ``usage_daily`` has no
role to go to yet (a restricted app role arrives with 0010).

Downgrade is only guaranteed on a database without data for this migration: it REFUSES while
any user is erased or scrubbed (``email``/``password_hash`` NULL or ``deleted_at`` set) —
re-adding NOT NULL would fail, and dropping ``deleted_at`` would resurrect erased accounts.
Older Phase-6 tombstones (hard-deleted users) do not block it. It is never a rollback path.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0005_persistence_erasure"
down_revision: str | None = "0004_conv_titles_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_ROLES = ("secrag_purger", "secrag_backup")

# Tables created here, in creation order (dropped in reverse).
NEW_TABLES: dict[str, str] = {
    # One row (id = 1): which DATA_MASTER_KEY wrapped this database's user keys
    # (rag_app.keycheck). Written by the API on its first start after the R5-5 check.
    "master_key_fingerprint": """
        CREATE TABLE master_key_fingerprint (
            id smallint PRIMARY KEY CHECK (id = 1),
            fingerprint varchar NOT NULL,
            algorithm varchar NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
    # One row per purger run (11.2b); no personal data.
    "purger_runs": """
        CREATE TABLE purger_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            requests_processed integer NOT NULL DEFAULT 0,
            errors integer NOT NULL DEFAULT 0,
            last_error text
        )""",
    # Global daily answer counter (R6-1); no personal data.
    "usage_daily": """
        CREATE TABLE usage_daily (
            day date PRIMARY KEY,
            answers integer NOT NULL DEFAULT 0 CHECK (answers >= 0),
            tokens bigint NOT NULL DEFAULT 0 CHECK (tokens >= 0)
        )""",
}

# secrag_purger (migration map, 0005). `UPDATE (id)` on the leaf tables lets the purger lock
# rows with `FOR UPDATE SKIP LOCKED` without being able to change their content. purger_runs
# also gets SELECT: `UPDATE … WHERE id = …` reads the column it filters on.
PURGER_GRANTS: dict[str, str] = {
    "messages": "SELECT, DELETE, UPDATE (id)",
    "conversations": "SELECT, DELETE, UPDATE (id)",
    "email_verification_tokens": "SELECT, DELETE, UPDATE (id)",
    "users": "SELECT, UPDATE, DELETE",
    "deletion_requests": "SELECT, UPDATE",
    "user_keys": "SELECT",
    "purger_runs": "SELECT, INSERT, UPDATE",
}

# secrag_backup reads every table. db/roles.sql's default privileges already cover tables the
# owner creates; the explicit grant keeps pg_dump working even if another role migrates.
BACKUP_TABLES: tuple[str, ...] = tuple(NEW_TABLES)

TOMBSTONE_COLUMNS = """
    ALTER TABLE deletion_requests
        ADD COLUMN status varchar NOT NULL DEFAULT 'done'
            CHECK (status IN ('pending', 'running', 'done', 'failed')),
        ADD COLUMN progress jsonb NOT NULL DEFAULT '{}'::jsonb,
        ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        ADD COLUMN last_error text,
        ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
        ADD COLUMN completed_at timestamptz
"""

REFUSE_DOWNGRADE_SQL = (
    "SELECT EXISTS (SELECT 1 FROM users"
    " WHERE email IS NULL OR password_hash IS NULL OR deleted_at IS NOT NULL)"
)


def _require_roles() -> None:
    bind = op.get_bind()
    present = set(
        bind.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
            {"names": list(REQUIRED_ROLES)},
        ).scalars()
    )
    missing = [r for r in REQUIRED_ROLES if r not in present]
    if missing:
        raise RuntimeError(
            "0005 grants to database roles that do not exist: "
            + ", ".join(missing)
            + " — run db/roles.sql first (scripts/db/apply_roles.sh; compose db-roles)"
        )


def upgrade() -> None:
    _require_roles()

    # users: erasure scrubs email/password_hash in the request path and marks deleted_at.
    op.execute("ALTER TABLE users ALTER COLUMN email DROP NOT NULL")
    op.execute("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL")
    op.execute("ALTER TABLE users ADD COLUMN deleted_at timestamptz")

    # deletion_requests: purge bookkeeping. Existing tombstones come from the synchronous
    # Phase-6 erasure, which deleted everything at once: they are 'done' (completed when
    # requested); new rows default to 'pending'.
    op.execute(TOMBSTONE_COLUMNS)
    op.execute(
        "UPDATE deletion_requests SET completed_at = requested_at, updated_at = requested_at"
    )
    op.execute("ALTER TABLE deletion_requests ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute(
        "CREATE INDEX ix_deletion_requests_open ON deletion_requests (requested_at)"
        " WHERE status IN ('pending', 'running', 'failed')"
    )

    for ddl in NEW_TABLES.values():
        op.execute(ddl)

    for table, privileges in PURGER_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO secrag_purger")
    for table in BACKUP_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO secrag_backup")


def downgrade() -> None:
    if op.get_bind().execute(text(REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0005: erased or scrubbed users exist (email/password_hash"
            " NULL or deleted_at set). The downgrade is not a rollback path once erasure data"
            " exists — roll back images instead (ADR phase 11, Rollback)."
        )

    for table in PURGER_GRANTS:
        if table not in NEW_TABLES:
            op.execute(f"REVOKE ALL ON {table} FROM secrag_purger")
    for table in reversed(list(NEW_TABLES)):
        op.execute(f"DROP TABLE IF EXISTS {table}")

    op.execute("DROP INDEX IF EXISTS ix_deletion_requests_open")
    op.execute(
        "ALTER TABLE deletion_requests"
        " DROP COLUMN IF EXISTS completed_at, DROP COLUMN IF EXISTS updated_at,"
        " DROP COLUMN IF EXISTS last_error, DROP COLUMN IF EXISTS attempts,"
        " DROP COLUMN IF EXISTS progress, DROP COLUMN IF EXISTS status"
    )

    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL")
    op.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
