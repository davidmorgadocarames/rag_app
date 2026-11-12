"""admin MFA + login lockout + password reset (expand): TOTP secret + recovery codes
(T12.3.4, "part 1"), the login lockout counter (T12.4.2, "part 2") and password reset
tokens (T12.4.6, "part 3").

Revision ID: 0007_mfa
Revises: 0006_sessions_role
Create Date: 2026-11-10

Expand-only (docs/DEFINITION_OF_DONE.md, "Expand/contract"): every change is additive or
relaxes a constraint, so the previous image keeps working on the migrated schema.

Authored for 12b-A (T12.3.4, "part 1": TOTP secret + recovery codes); 12b-B (T12.4.2, "part
2": login lockout) and 12b-C (T12.4.6, "part 3": password reset tokens) EXTEND THIS SAME
FILE -- 0007 is not promoted to `main` yet, so there is no new revision number for either
part (0008 is reserved for Phase 13). ``0006 is NOT edited`` (it is 12a's file, already
shipped). Each part adds its own ``CREATE TABLE``/grants in ``upgrade()``, its own teardown
in ``downgrade()`` (leaf-first, mirroring ``upgrade``), and its own purge-step registration
(or X3 exemption) -- never touching the other parts' statements.

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

# --- part 2 (T12.4.2, 12b-B): login lockout --------------------------------------------

# Keyed by HMAC(LOCKOUT_HMAC_SECRET, email) -- rag_app.lockout.lockout_key -- never a
# user_id FK: a lockout row can legitimately exist for an email that is not (or is no
# longer) a registered account (enumeration-safe throttling, F5/API-6). See
# rag_app.erasure.X3_EXEMPTIONS for why this has no PURGE_STEPS entry.
LOGIN_LOCKOUTS_TABLE = """
    CREATE TABLE login_lockouts (
        email_hmac varchar PRIMARY KEY,
        failed_count integer NOT NULL DEFAULT 0,
        locked_until timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now()
    )"""

# No secrag_backup UPDATE needed: the retention purge below fully DELETEs stale rows, it
# never partially updates one. secrag_purger needs SELECT + DELETE, plus UPDATE on a column
# (never actually updated) because `SELECT ... FOR UPDATE SKIP LOCKED` itself requires the
# UPDATE privilege on the locked rows -- same reasoning as 0007 part 1's `UPDATE (user_id)`
# grant on `user_mfa` (comment at the top of this file).
PART2_GRANTS = (
    "GRANT SELECT, DELETE, UPDATE (email_hmac) ON login_lockouts TO secrag_purger",
    "GRANT SELECT ON login_lockouts TO secrag_backup",
)

PART2_REFUSE_DOWNGRADE_SQL = "SELECT EXISTS (SELECT 1 FROM login_lockouts)"

# --- part 3 (T12.4.6, 12b-C): password reset tokens --------------------------------------

# Same storage convention as part 1's mfa_recovery_codes / 0003's email_verification_tokens:
# only the SHA-256 hash of the raw token is ever stored (rag_app.security.hash_token).
PASSWORD_RESET_TOKENS_TABLE = """
    CREATE TABLE password_reset_tokens (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        token_hash varchar NOT NULL UNIQUE,
        expires_at timestamptz NOT NULL,
        used_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )"""

PART3_GRANTS = (
    "GRANT SELECT, DELETE, UPDATE (id) ON password_reset_tokens TO secrag_purger",
    "GRANT SELECT ON password_reset_tokens TO secrag_backup",
)

PART3_REFUSE_DOWNGRADE_SQL = "SELECT EXISTS (SELECT 1 FROM password_reset_tokens)"


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

    # --- part 2 (T12.4.2): login lockout ---
    op.execute(LOGIN_LOCKOUTS_TABLE)
    for grant in PART2_GRANTS:
        op.execute(grant)

    # --- part 3 (T12.4.6): password reset tokens ---
    op.execute(PASSWORD_RESET_TOKENS_TABLE)
    op.execute("CREATE INDEX ix_password_reset_tokens_user_id ON password_reset_tokens (user_id)")
    for grant in PART3_GRANTS:
        op.execute(grant)


def downgrade() -> None:
    if op.get_bind().execute(text(PART3_REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0007: a password reset token exists. The downgrade is not"
            " a rollback path once that data exists — roll back images instead (ADR phase 11,"
            " Rollback)."
        )
    if op.get_bind().execute(text(PART2_REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0007: a login lockout row exists. The downgrade is not a"
            " rollback path once that data exists — roll back images instead (ADR phase 11,"
            " Rollback)."
        )
    if op.get_bind().execute(text(PART1_REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0007: an MFA enrolment or recovery code exists. The"
            " downgrade is not a rollback path once that data exists — roll back images"
            " instead (ADR phase 11, Rollback)."
        )

    # Leaf-first, mirroring upgrade() in reverse: part 3, then part 2, then part 1.
    op.execute("REVOKE ALL ON password_reset_tokens FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS password_reset_tokens")
    op.execute("REVOKE ALL ON login_lockouts FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS login_lockouts")
    op.execute("REVOKE ALL ON mfa_recovery_codes FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS mfa_recovery_codes")
    op.execute("REVOKE ALL ON user_mfa FROM secrag_purger, secrag_backup")
    op.execute("DROP TABLE IF EXISTS user_mfa")
