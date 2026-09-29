"""SQLAlchemy models for the corpus (documents and chunks).

The ``chunks.tsv`` column (full-text search vector for BM25) is created as a
generated column in the Alembic migration and is not mapped here; hybrid
retrieval reads it via raw SQL.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024  # bge-m3 dense embedding dimension


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str] = mapped_column(String)
    effective_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    category_rank: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_uid: Mapped[str] = mapped_column(String, unique=True, index=True)
    heading: Mapped[str] = mapped_column(Text, default="")
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    version: Mapped[str] = mapped_column(String, index=True)
    effective_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    document: Mapped[Document] = relationship(back_populates="chunks")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable since 0005: the erasure request path scrubs them (PII) before the batched
    # purge removes the row (11.2b). A live account always has both.
    email: Mapped[str | None] = mapped_column(String, unique=True, index=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Set by the erasure request path (0005); login and token checks ignore such users.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    key: Mapped[UserKey | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserKey(Base):
    """Per-user data key, wrapped by the master key. Deleting this row crypto-shreds."""

    __tablename__ = "user_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="key")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Optional user-set title, encrypted with the per-user key (null -> derived from
    # the first message so nothing is stored in the clear).
    title_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String)
    content_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


TOMBSTONE_STATUSES = ("pending", "running", "done", "failed")


class DeletionRequest(Base):
    """Tombstone for a GDPR erasure. Contains no personal data; kept for replay.

    0005 adds the purge bookkeeping (status, per-step progress, attempts, last error class)
    used by the batched purger (11.2b)."""

    __tablename__ = "deletion_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, index=True)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String, server_default="pending")
    progress: Mapped[dict[str, int]] = mapped_column(JSONB, server_default="{}")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PurgerRun(Base):
    """One run of the batched purger (0005; written by 11.2b). No personal data."""

    __tablename__ = "purger_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requests_processed: Mapped[int] = mapped_column(Integer, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UsageDaily(Base):
    """Global daily answer counter for the daily cap (R6-1, 0005). No personal data."""

    __tablename__ = "usage_daily"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    answers: Mapped[int] = mapped_column(Integer, server_default="0")
    tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")


class MasterKeyFingerprint(Base):
    """The one row identifying the DATA_MASTER_KEY this database was initialised with
    (0005; see rag_app.keycheck)."""

    __tablename__ = "master_key_fingerprint"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String)
    algorithm: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
