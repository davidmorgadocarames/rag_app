"""server-side sessions + users.role (expand): ``sessions`` table (T12.1.1, P12-D3 timeouts,
``mfa_verified_at`` for 12b), ``users.role`` (``user``/``admin``); per-role grants.

Revision ID: 0006_sessions_role
Revises: 0005_persistence_erasure
Create Date: 2026-10-16

Expand-only (docs/DEFINITION_OF_DONE.md, "Expand/contract"): every change is additive or
relaxes a constraint, so the previous image keeps working on the migrated schema.

Written for 12a (T12.1.1); 12b (T12.3.4, migration 0007) must NOT edit this file — it adds
its own tables (TOTP secret, recovery codes, lockout, reset tokens) in 0007. ``mfa_verified_at``
is added here, empty, so 0007's step-up flow has a column to set without touching 0006.

Grants follow PHASE_PLANNING's migration map (X4), same pattern as 0005: ``secrag_purger``
gets the leaf-table triple (``SELECT, DELETE, UPDATE (id)`` — the ``UPDATE (id)`` is for the
row lock taken by ``FOR UPDATE SKIP LOCKED`` in the purge-step batch delete, same as
``messages``/``conversations``/``email_verification_tokens``); ``secrag_backup`` reads it via
the default privileges from ``db/roles.sql`` (W16) — the explicit grant below keeps a direct
``pg_dump`` working even without them.

Downgrade refuses while any user has a non-default ``role`` (losing an admin assignment would
be a real data loss, not just an inconvenience); it is never a rollback path once such data
exists — roll back images instead (ADR phase 11, Rollback; 0006 stays after the 12a rollback
per PHASE_TASKS T12.6a.2).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0006_sessions_role"
down_revision: str | None = "0005_persistence_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_ROLES = ("secrag_purger", "secrag_backup")

SESSIONS_TABLE = """
    CREATE TABLE sessions (
        id varchar PRIMARY KEY,
        user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at timestamptz NOT NULL,
        last_seen_at timestamptz NOT NULL,
        idle_expires_at timestamptz NOT NULL,
        absolute_expires_at timestamptz NOT NULL,
        mfa_verified_at timestamptz
    )"""

# secrag_purger (migration map, 0006): same leaf-table shape as 0005's PURGER_GRANTS.
SESSIONS_PURGER_GRANT = "GRANT SELECT, DELETE, UPDATE (id) ON sessions TO secrag_purger"

REFUSE_DOWNGRADE_SQL = "SELECT EXISTS (SELECT 1 FROM users WHERE role <> 'user')"


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
            "0006 grants to database roles that do not exist: "
            + ", ".join(missing)
            + " — run db/roles.sql first (scripts/db/apply_roles.sh; compose db-roles)"
        )


def upgrade() -> None:
    _require_roles()

    op.execute(
        "ALTER TABLE users ADD COLUMN role varchar NOT NULL DEFAULT 'user'"
        " CHECK (role IN ('user', 'admin'))"
    )
    op.execute(SESSIONS_TABLE)
    op.execute("CREATE INDEX ix_sessions_user_id ON sessions (user_id)")

    op.execute(SESSIONS_PURGER_GRANT)
    op.execute("GRANT SELECT ON sessions TO secrag_backup")


def downgrade() -> None:
    if op.get_bind().execute(text(REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0006: a user has a non-default role (e.g. 'admin')."
            " The downgrade is not a rollback path once that data exists — roll back images"
            " instead (ADR phase 11, Rollback)."
        )

    op.execute("REVOKE ALL ON sessions FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS role")
