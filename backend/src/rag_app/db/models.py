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
    # 0006 (T12.1.1): CHECK (role IN ('user', 'admin')) in the migration; no API path sets it
    # (the admin bootstrap CLI, T12.3.3, is the only writer). P12-D3 reads it to pick the
    # shorter admin session timeouts.
    role: Mapped[str] = mapped_column(String, server_default="user", default="user")
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
    # T14.2.4 (0009, block 14-C1): the running conversation memory -- a bounded, structured
    # JSON summary (`rag_app.assistant.memory.ConversationSummary`), encrypted the same way
    # as `title_encrypted`. Null until the first successful summary write (or whenever one
    # has been dropped as invalid/unreachable); purged with the row itself (no separate
    # purge step -- see 0009's own docstring).
    summary_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
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


class UserSession(Base):
    """Server-side session (0006, T12.1.1/T12.1.2): the cookie carries the raw, random ID —
    only its SHA-256 hash is the primary key here (``rag_app.security.hash_token``), so a
    stolen row of this table never yields a working session. ``idle_expires_at`` is pushed
    forward on every throttled touch; ``absolute_expires_at`` is fixed at creation (P12-D3).
    ``mfa_verified_at`` arrives empty in 12a, set by the 12b step-up flow."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    mfa_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserMfa(Base):
    """TOTP enrolment (0007 "part 1", T12.3.4): one row per user, like ``UserKey``.
    ``secret_encrypted`` is the TOTP secret wrapped with ``DATA_MASTER_KEY`` (defence in
    depth; crypto-shredding ``user_keys`` already makes the account's data unreadable).
    ``enabled`` only flips true once ``POST /account/mfa/confirm`` verifies a real code."""

    __tablename__ = "user_mfa"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MfaRecoveryCode(Base):
    """Single-use TOTP recovery codes (0007 "part 1", T12.3.4): only ``code_hash``
    (SHA-256, ``rag_app.security.hash_token``) is ever stored -- the plaintext codes are
    shown once at confirm time and never logged."""

    __tablename__ = "mfa_recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoginLockout(Base):
    """Login lockout counter (0007 "part 2", T12.4.2): keyed by
    ``HMAC(LOCKOUT_HMAC_SECRET, email)`` (``rag_app.lockout.lockout_key``) -- NOT a
    ``user_id`` FK, so a login attempt against an email that is not even a registered
    account is throttled exactly like a real one (enumeration-safe). See
    ``rag_app.lockout`` for the atomic counter/back-off and ``rag_app.erasure.
    X3_EXEMPTIONS`` for why this table has no ``PURGE_STEPS`` entry."""

    __tablename__ = "login_lockouts"

    email_hmac: Mapped[str] = mapped_column(String, primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
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


class AdminAction(Base):
    """Audit trail for every mutating ``/admin/*`` call (0008, T13.1.6): one row per admin
    POST, written in the SAME request. ``target`` is always an OPAQUE identifier (an erasure
    request id -- never an email or any other raw value a client submitted) -- the same
    "no PII in the admin API" rule the dashboard itself upholds (T13.1.7).

    ``admin_user_id`` survives the acting admin's own later erasure: ``ON DELETE SET NULL``
    here is defence in depth, and ``rag_app.erasure.PURGE_STEPS`` carries the primary,
    batched "pseudonymisation" step (nulls it leaf-first, before the admin's ``users`` row is
    removed) -- the audit row itself is KEPT, only the link to the (now-gone) admin account
    is severed; see ``rag_app.erasure.X3_EXEMPTIONS``' module docstring for why some
    user-linked columns are handled this way instead of a delete."""

    __tablename__ = "admin_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DefenceRun(Base):
    """One probe result from the defence lab (0008; the lab itself is 13-D -- this table is
    created now so 13-D needs no further migration). Keyed by a ``docs/security/
    THREAT_MODEL.md`` probe id; ``evidence`` is operator-written, PII-free by construction
    (the lab only ever targets its own dedicated, disposable test accounts). No personal
    data -- never a ``PURGE_STEPS`` entry."""

    __tablename__ = "defence_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    probe_id: Mapped[str] = mapped_column(String, index=True)
    git_sha: Mapped[str] = mapped_column(String)
    expected_outcome: Mapped[str] = mapped_column(String)
    observed_outcome: Mapped[str] = mapped_column(String)
    passed: Mapped[bool] = mapped_column(Boolean)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PasswordResetToken(Base):
    """Password reset tokens (0007 "part 3", T12.4.6): hashed, single-use, 30-minute TTL --
    same storage convention as ``EmailVerificationToken`` (only ``token_hash``, SHA-256 via
    ``rag_app.security.hash_token``, is ever stored). Consumed by exactly ONE atomic
    ``UPDATE ... WHERE used_at IS NULL AND expires_at > :now`` (X10, see
    ``rag_app.api.auth._consume_password_reset_token``), never a SELECT-then-UPDATE."""

    __tablename__ = "password_reset_tokens"

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
