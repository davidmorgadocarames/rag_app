"""conversation titles + per-message token counts

Revision ID: 0004_conv_titles_tokens
Revises: 0003_email_verification
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_conv_titles_tokens"
down_revision: str | None = "0003_email_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Optional user-set title, encrypted with the per-user key (null -> derive from
    # the first message). Per-message token counts back the "conversation total" UI.
    op.execute("ALTER TABLE conversations ADD COLUMN title_encrypted bytea")
    op.execute("ALTER TABLE messages ADD COLUMN prompt_tokens integer")
    op.execute("ALTER TABLE messages ADD COLUMN completion_tokens integer")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS completion_tokens")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS prompt_tokens")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS title_encrypted")
