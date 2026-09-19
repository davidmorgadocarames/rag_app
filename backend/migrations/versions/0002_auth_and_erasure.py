"""auth + GDPR erasure: users, user_keys, conversations, messages, deletion_requests

Revision ID: 0002_auth_and_erasure
Revises: 0001_initial
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_auth_and_erasure"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            email varchar NOT NULL UNIQUE,
            password_hash varchar NOT NULL,
            email_verified boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE user_keys (
            user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            wrapped_key bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE conversations (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_conversations_user_id ON conversations (user_id)")
    op.execute(
        """
        CREATE TABLE messages (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role varchar NOT NULL,
            content_encrypted bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_messages_conversation_id ON messages (conversation_id)")
    op.execute(
        """
        CREATE TABLE deletion_requests (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL UNIQUE,
            requested_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_deletion_requests_user_id ON deletion_requests (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deletion_requests")
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversations")
    op.execute("DROP TABLE IF EXISTS user_keys")
    op.execute("DROP TABLE IF EXISTS users")
