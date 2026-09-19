"""initial schema: documents and chunks (pgvector + full-text)

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY,
            slug varchar NOT NULL UNIQUE,
            source varchar,
            title varchar,
            version varchar NOT NULL,
            effective_date date,
            category_rank varchar,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE chunks (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_uid varchar NOT NULL UNIQUE,
            heading text NOT NULL DEFAULT '',
            ordinal integer NOT NULL,
            text text NOT NULL,
            embedding vector(1024) NOT NULL,
            version varchar NOT NULL,
            effective_date date,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
        )
        """
    )

    op.execute("CREATE INDEX ix_chunks_document_id ON chunks (document_id)")
    op.execute("CREATE INDEX ix_chunks_version ON chunks (version)")
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING GIN (tsv)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks " "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
