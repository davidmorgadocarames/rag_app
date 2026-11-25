"""conversation memory: conversations.summary_encrypted

Revision ID: 0009_conversation_summary
Revises: 0008_admin_dashboard
Create Date: 2026-11-25

T14.2.4 (block 14-C1): a single bounded, structured, per-user-encrypted running summary of
the conversation (``rag_app.assistant.memory.ConversationSummary`` -- topics/entities/
owasp_version/open_questions, JSON, serialized with the SAME Fernet per-user key scheme
``title_encrypted`` already uses (``rag_app.crypto.encrypt``/``decrypt`` -- no new crypto
primitive). Nullable: most conversations have no summary yet (brand new, or every attempt to
write one has failed/dropped).

Expand-only (docs/DEFINITION_OF_DONE.md, "Expand/contract"): a plain, nullable column add;
the previous image reads/writes `conversations` exactly as before (it simply never sets or
reads this column).

No grant statement needed (X4 migration map): `conversations` already has its table-level
grants from 0005 (``secrag_purger``'s leaf-first DELETE, ``secrag_backup``'s SELECT via
`GRANT SELECT ON ALL TABLES` + default privileges, db/roles.sql) -- a new COLUMN on an
already-granted table needs nothing new, same precedent as 0004's `title_encrypted`.

Purge (X3): no new `PURGE_STEPS` entry -- `summary_encrypted` is plain content on the
`conversations` table, not a user-linked column (no `user_id`/`*_user_id`/`admin_id`/`*_hmac`
name, no FK to users/conversations/messages), so `uncovered_user_columns`'s scans do not (and
should not) flag it; the existing "conversations" purge step already DELETEs the whole row
(leaf-first, after its messages) once the account is erased -- the summary disappears with
it, with no separate step, exactly like every other column already on that table
(`title_encrypted`, `created_at`).

Downgrade refuses while any conversation holds a (non-null) summary: losing conversation
memory content silently would be a real regression, not just an inconvenience -- same
"never a rollback path once such data exists" discipline as 0008's `admin_actions`/
`defence_runs`; roll back images instead (ADR phase 11, Rollback).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0009_conversation_summary"
down_revision: str | None = "0008_admin_dashboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFUSE_DOWNGRADE_SQL = (
    "SELECT EXISTS (SELECT 1 FROM conversations WHERE summary_encrypted IS NOT NULL)"
)


def upgrade() -> None:
    op.execute("ALTER TABLE conversations ADD COLUMN summary_encrypted bytea")


def downgrade() -> None:
    if op.get_bind().execute(text(REFUSE_DOWNGRADE_SQL)).scalar_one():
        raise RuntimeError(
            "refusing to downgrade 0009: a conversation holds a summary. The downgrade is"
            " not a rollback path once such data exists — roll back images instead (ADR"
            " phase 11, Rollback)."
        )
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS summary_encrypted")
