"""admin dashboard (expand): admin_actions audit trail + defence_runs history

Revision ID: 0008_admin_dashboard
Revises: 0007_mfa
Create Date: 2026-11-15

Expand-only (docs/DEFINITION_OF_DONE.md, "Expand/contract"): every change is additive, so
the previous image keeps working on the migrated schema.

Authored for 13-B (T13.1.6). ``admin_actions``: one row per mutating ``/admin/*`` call
(``rag_app.admin_audit.record_admin_action``) -- ``target`` is always an opaque identifier
(an erasure request id), never raw PII a client submitted (T13.1.7). ``admin_user_id``
survives the acting admin's own later erasure (``ON DELETE SET NULL`` here; the PRIMARY
mechanism is the batched, leaf-first "admin_actions" step in ``rag_app.erasure.PURGE_STEPS``,
which nulls it before the admin's own ``users`` row is removed -- same split as the
"sessions" purge step vs. the request path's immediate revoke-all). ``defence_runs``: history
for the defence lab (13-D implements the probes that write to it; the table is created now so
13-D needs no further migration) -- no personal data, never a purge step.

Grants follow PHASE_PLANNING's migration map (X4), same leaf-table convention as 0005/0007:
``secrag_purger`` gets ``SELECT, UPDATE (admin_user_id)`` on ``admin_actions`` (the
``UPDATE`` is for both the pseudonymisation itself and the row lock ``FOR UPDATE SKIP
LOCKED`` takes in the purge-step batch -- never ``DELETE``, this step never removes a row);
``secrag_backup`` reads both new tables.

Downgrade is blocked while either table holds any row: losing the admin audit trail or the
defence-lab history silently would be a real regression, not just an inconvenience -- never a
rollback path once such data exists; roll back images instead (ADR phase 11, Rollback).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0008_admin_dashboard"
down_revision: str | None = "0007_mfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_ROLES = ("secrag_purger", "secrag_backup")

ADMIN_ACTIONS_TABLE = """
    CREATE TABLE admin_actions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        admin_user_id uuid REFERENCES users (id) ON DELETE SET NULL,
        action varchar NOT NULL,
        target varchar,
        result varchar NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )"""

DEFENCE_RUNS_TABLE = """
    CREATE TABLE defence_runs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        probe_id varchar NOT NULL,
        git_sha varchar NOT NULL,
        expected_outcome varchar NOT NULL,
        observed_outcome varchar NOT NULL,
        passed boolean NOT NULL,
        evidence text,
        run_at timestamptz NOT NULL DEFAULT now()
    )"""

GRANTS = (
    "GRANT SELECT, UPDATE (admin_user_id) ON admin_actions TO secrag_purger",
    "GRANT SELECT ON admin_actions TO secrag_backup",
    "GRANT SELECT ON defence_runs TO secrag_backup",
)

REFUSE_DOWNGRADE_SQL = (
    "SELECT EXISTS (SELECT 1 FROM admin_actions) OR EXISTS (SELECT 1 FROM defence_runs)"
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
            "0008 grants to database roles that do not exist: "
            + ", ".join(missing)
            + " — run db/roles.sql first (scripts/db/apply_roles.sh; compose db-roles)"
        )


def upgrade() -> None:
    _require_roles()

    op.execute(ADMIN_ACTIONS_TABLE)
    op.execute("CREATE INDEX ix_admin_actions_admin_user_id ON admin_actions (admin_user_id)")
    op.execute("CREATE INDEX ix_admin_actions_created_at ON admin_actions (created_at)")

    op.execute(DEFENCE_RUNS_TABLE)
    op.execute("CREATE INDEX ix_defence_runs_probe_id ON defence_runs (probe_id)")
    op.execute("CREATE INDEX ix_defence_runs_run_at ON defence_runs (run_at)")

    for grant in GRANTS:
        op.execute(grant)


def downgrade() -> None:
    if op.get_bind().execute(text(REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0008: an admin action or defence run exists. The downgrade"
            " is not a rollback path once that data exists — roll back images instead (ADR"
            " phase 11, Rollback)."
        )

    op.execute("REVOKE ALL ON defence_runs FROM secrag_backup")
    op.execute("DROP TABLE IF EXISTS defence_runs")
    op.execute("REVOKE ALL ON admin_actions FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS admin_actions")
