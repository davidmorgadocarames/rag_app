"""admin MFA (expand): TOTP secret + single-use recovery codes (T12.3.4, "part 1").

Revision ID: 0007_mfa
Revises: 0006_sessions_role
Create Date: 2026-11-10

Expand-only (docs/DEFINITION_OF_DONE.md, "Expand/contract"): every change is additive or
relaxes a constraint, so the previous image keeps working on the migrated schema.

Authored for 12b-A (T12.3.4, "part 1": TOTP secret + recovery codes). ``0006 is NOT edited``
(it is 12a's file, already shipped). Later 12b blocks on the SAME branch EXTEND THIS FILE
with their own part before it is ever merged to ``main``:
  - 12b-B (T12.4.2, "part 2"): lockout table, keyed by ``HMAC(secret, email)``.
  - 12b-C (T12.4.6, "part 3"): password reset tokens.
Each part adds its own ``CREATE TABLE``/grants in ``upgrade()``, its own teardown in
``downgrade()`` (leaf-first, mirroring ``upgrade``), and its own purge-step registration in
``rag_app.erasure.PURGE_STEPS`` -- never touching the other parts' statements.

``user_mfa`` is a one-row-per-user table (like ``user_keys``): ``secret_encrypted`` is the
TOTP secret wrapped with ``DATA_MASTER_KEY`` (``rag_app.crypto``, same crypto-shred guarantee
as the user's data key -- deleting ``user_keys`` already makes everything unreadable, this
is defence in depth for the one secret that is not itself ciphertext of user content).
``enabled`` only flips to true once the user proves they can generate a valid code
(``POST /account/mfa/confirm`` in T12.3.5); a never-confirmed pending secret grants nothing.

``mfa_recovery_codes`` stores only a SHA-256 hash of each code (``rag_app.security.
hash_token``, same convention as e-mail verification/session tokens) -- the plaintext codes
are shown to the user exactly once, at confirm time, and never stored or logged.

Grants follow PHASE_PLANNING's migration map (X4), same leaf-table triple as 0005/0006:
``secrag_purger`` gets ``SELECT, DELETE, UPDATE (id)`` (the ``UPDATE (id)`` is for the row
lock ``FOR UPDATE SKIP LOCKED`` takes in the purge-step batch delete); ``secrag_backup``
reads both via the default privileges from ``db/roles.sql`` (W16) -- the explicit grant below
keeps a direct ``pg_dump`` working even without them.

Downgrade of this file as a whole is blocked while this part's data exists (any row in
either table): losing an MFA enrolment or recovery codes silently would be a real security
regression, not just an inconvenience -- never a rollback path once such data exists; roll
back images instead (ADR phase 11, Rollback; mirrors 0006's rule for ``role``).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0007_mfa"
down_revision: str | None = "0006_sessions_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_ROLES = ("secrag_purger", "secrag_backup")

USER_MFA_TABLE = """
    CREATE TABLE user_mfa (
        user_id uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
        secret_encrypted bytea NOT NULL,
        enabled boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        confirmed_at timestamptz
    )"""

MFA_RECOVERY_CODES_TABLE = """
    CREATE TABLE mfa_recovery_codes (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        code_hash varchar NOT NULL UNIQUE,
        used_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )"""

PART1_GRANTS = (
    "GRANT SELECT, DELETE, UPDATE (user_id) ON user_mfa TO secrag_purger",
    "GRANT SELECT ON user_mfa TO secrag_backup",
    "GRANT SELECT, DELETE, UPDATE (id) ON mfa_recovery_codes TO secrag_purger",
    "GRANT SELECT ON mfa_recovery_codes TO secrag_backup",
)

PART1_REFUSE_DOWNGRADE_SQL = (
    "SELECT EXISTS (SELECT 1 FROM user_mfa) OR EXISTS (SELECT 1 FROM mfa_recovery_codes)"
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
            "0007 grants to database roles that do not exist: "
            + ", ".join(missing)
            + " — run db/roles.sql first (scripts/db/apply_roles.sh; compose db-roles)"
        )


def upgrade() -> None:
    _require_roles()

    # --- part 1 (T12.3.4): TOTP secret + recovery codes ---
    op.execute(USER_MFA_TABLE)
    op.execute("CREATE INDEX ix_user_mfa_user_id ON user_mfa (user_id)")
    op.execute(MFA_RECOVERY_CODES_TABLE)
    op.execute("CREATE INDEX ix_mfa_recovery_codes_user_id ON mfa_recovery_codes (user_id)")
    for grant in PART1_GRANTS:
        op.execute(grant)

    # 12b-B (part 2, lockout) and 12b-C (part 3, reset tokens) add their CREATE TABLE/grants
    # here, below this line -- never above (leaf-first ordering of downgrade mirrors it).


def downgrade() -> None:
    if op.get_bind().execute(text(PART1_REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0007: an MFA enrolment or recovery code exists. The"
            " downgrade is not a rollback path once that data exists — roll back images"
            " instead (ADR phase 11, Rollback)."
        )

    op.execute("REVOKE ALL ON mfa_recovery_codes FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS mfa_recovery_codes")
    op.execute("REVOKE ALL ON user_mfa FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS user_mfa")
