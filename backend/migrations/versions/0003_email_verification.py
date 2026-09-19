"""email verification tokens

Revision ID: 0003_email_verification
Revises: 0002_auth_and_erasure
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_email_verification"
down_revision: str | None = "0002_auth_and_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE email_verification_tokens (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash varchar NOT NULL UNIQUE,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_evt_user_id ON email_verification_tokens (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS email_verification_tokens")
