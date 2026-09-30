"""GDPR erasure — asynchronous since 11.2b (D-ER; ADR phase 6 as refined by ADR phase 11).

1. **Request path** (``request_erasure``, ``DELETE /account``): ONE short transaction with
   ``SET LOCAL lock_timeout``/``statement_timeout`` — delete the ``user_keys`` row
   (crypto-shred: every ciphertext of the user becomes unreadable at once), scrub
   ``email``/``password_hash``, set ``deleted_at``, write the tombstone ``pending``. It never
   touches the user's conversations or messages, so its cost does not grow with the history.
   No tombstone export here (the purger exports).
2. **Purger** (``rag_app.purger``; ``python -m rag_app.erasure purge``): removes the remaining
   rows in batches, leaf-first, following ``PURGE_STEPS``, then the ``users`` row, and marks
   the tombstone ``done`` — within ``PURGE_DEADLINE_HOURS`` (hourly Job on Azure, a compose
   loop locally).
3. **Restore** (``replay_deletions``, run by ``scripts/db/restore.sh`` as the OWNER — it
   deletes ``user_keys``): re-applies step 1 to every tombstoned account a restore brought
   back and re-queues it ``pending``; ``purge_orphaned`` enqueues users without a key.

``erase_user`` (synchronous hard delete, tombstone ``done``) is kept only for the offline tool
that runs with no concurrent traffic (``scripts/azure/key_recovery.py``). The API never uses it.

**Purge-step registry (X3).** Every table with a user-linked column (``user_id``,
``*_user_id``, ``admin_id``, ``*_hmac``) needs a step in ``PURGE_STEPS`` or a documented entry
in ``X3_EXEMPTIONS``; ``uncovered_user_columns`` scans ``information_schema`` and a test fails
on anything unregistered. A new table adds its step here (leaf-first: before ``users``) and
its purger grants in its migration (PHASE_PLANNING migration map).

    python -m rag_app.erasure purge [--batch-size N] [--max-seconds S] [--no-export]
    python -m rag_app.erasure purge-now <request-id>
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CursorResult, Result, delete, exists, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from rag_app.db.models import DeletionRequest, User, UserKey
from rag_app.retention import BACKUP_RETENTION_DAYS, TOMBSTONE_EXPORT_RETENTION_DAYS

# The promise of the 202 message (PHASE_PLANNING 11.2b): the purger removes the remaining
# encrypted rows within this many hours (an hourly Job, well inside the deadline).
PURGE_DEADLINE_HOURS = 24

# The request path's short transaction: never wait long for a lock, never run long.
REQUEST_LOCK_TIMEOUT = "2s"
REQUEST_STATEMENT_TIMEOUT = "5s"

# DA-G1-4: the request path's lock/statement timeout (or a cancel) is retryable — 503 with
# Retry-After, nothing changed (the transaction rolled back).
ERASURE_RETRYABLE_SQLSTATES = frozenset({"55P03", "57014"})
ERASURE_RETRY_AFTER_SECONDS = 5

# The 202 text (DA-G1-9 i/ii): "unreadable at once" holds for the live service; the backups
# still hold the wrapped key and the email until they expire; the kept tombstone record and
# its exports are disclosed. PRD F7, ADR phase 6 and the account page say the same.
ERASURE_ACCEPTED_MESSAGE = (
    "Account deleted. Your data in the live service is unreadable from now on, and the"
    f" remaining encrypted rows are removed within {PURGE_DEADLINE_HOURS} h. Encrypted backup"
    " copies still hold your encrypted data, its wrapped key and your email address until"
    f" they are deleted, within {BACKUP_RETENTION_DAYS} days. A minimal erasure record"
    " (a random id, dates and a status; no email or content) is kept so that a restore from"
    " backup cannot bring the account back; its exported copies are kept for"
    f" {TOMBSTONE_EXPORT_RETENTION_DAYS} days."
)

OPEN_STATUSES = ("pending", "running", "failed")


@dataclass(frozen=True)
class PurgeStep:
    """One leaf-first purge step: deletes up to ``:n`` rows of ``table`` owned by ``:uid``
    with ``FOR UPDATE SKIP LOCKED`` (never waits for a row another transaction holds);
    ``remaining`` tells whether rows are left (e.g. skipped because locked)."""

    name: str
    table: str
    delete_batch: str
    remaining: str


PURGE_STEPS: tuple[PurgeStep, ...] = (
    PurgeStep(
        name="messages",
        table="messages",
        delete_batch="""
            WITH victim AS (
                SELECT m.id FROM messages m
                 WHERE m.conversation_id IN (SELECT c.id FROM conversations c
                                              WHERE c.user_id = :uid)
                 LIMIT :n FOR UPDATE OF m SKIP LOCKED)
            DELETE FROM messages WHERE id IN (SELECT id FROM victim)""",
        remaining="""
            SELECT EXISTS (SELECT 1 FROM messages m
                             JOIN conversations c ON c.id = m.conversation_id
                            WHERE c.user_id = :uid)""",
    ),
    PurgeStep(
        name="conversations",
        table="conversations",
        # Only conversations already without messages: a delete never cascades into an
        # unbounded number of rows (a message written late is caught by the next pass).
        delete_batch="""
            WITH victim AS (
                SELECT c.id FROM conversations c
                 WHERE c.user_id = :uid
                   AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
                 LIMIT :n FOR UPDATE SKIP LOCKED)
            DELETE FROM conversations WHERE id IN (SELECT id FROM victim)""",
        remaining="SELECT EXISTS (SELECT 1 FROM conversations WHERE user_id = :uid)",
    ),
    PurgeStep(
        name="email_verification_tokens",
        table="email_verification_tokens",
        delete_batch="""
            WITH victim AS (
                SELECT id FROM email_verification_tokens WHERE user_id = :uid
                 LIMIT :n FOR UPDATE SKIP LOCKED)
            DELETE FROM email_verification_tokens WHERE id IN (SELECT id FROM victim)""",
        remaining="SELECT EXISTS (SELECT 1 FROM email_verification_tokens WHERE user_id = :uid)",
    ),
    # Sessions / lockout rows join here (and the request path) when 12a/12b add their tables.
    PurgeStep(
        name="users",
        table="users",
        # Last, and only an account the request path marked deleted.
        delete_batch="""
            WITH victim AS (
                SELECT id FROM users WHERE id = :uid AND deleted_at IS NOT NULL
                 FOR UPDATE SKIP LOCKED)
            DELETE FROM users WHERE id IN (SELECT id FROM victim)""",
        remaining="SELECT EXISTS (SELECT 1 FROM users WHERE id = :uid)",
    ),
)

# (table, column) → why it has no purge step (X3; PHASE_PLANNING migration map, 0005).
X3_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("deletion_requests", "user_id"): "the opaque tombstone kept for replay after a restore",
    ("user_keys", "user_id"): "deleted in the request path's short transaction"
    " (crypto-shred); replay_deletions deletes it as the owner",
}

X3_SCAN_SQL = r"""
    SELECT table_name::text, column_name::text FROM information_schema.columns
     WHERE table_schema = 'public'
       AND (column_name IN ('user_id', 'admin_id')
            OR column_name LIKE '%\_user\_id' ESCAPE '\'
            OR column_name LIKE '%\_hmac' ESCAPE '\')
     ORDER BY 1, 2"""


def uncovered_user_columns(session: Session) -> list[str]:
    """``table.column`` of every user-linked column with neither a purge step nor an
    exemption (X3). Empty = covered."""
    covered = {step.table for step in PURGE_STEPS}
    return [
        f"{table}.{column}"
        for table, column in session.execute(text(X3_SCAN_SQL)).all()
        if table not in covered and (table, column) not in X3_EXEMPTIONS
    ]


def _rowcount(result: Result[Any]) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _enqueue(session: Session, user_id: uuid.UUID, now: dt.datetime) -> None:
    """Tombstone ``pending`` — a new one, or the existing one re-queued (``requested_at``
    and ``attempts`` kept)."""
    stmt = pg_insert(DeletionRequest).values(
        id=uuid.uuid4(),
        user_id=user_id,
        requested_at=now,
        status="pending",
        progress={},
        attempts=0,
        updated_at=now,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[DeletionRequest.user_id],
            set_={"status": "pending", "updated_at": now, "completed_at": None, "last_error": None},
        )
    )


def _short_step(session: Session, user_id: uuid.UUID, now: dt.datetime) -> bool:
    """Crypto-shred + PII scrub + ``deleted_at`` + tombstone ``pending`` (no commit).
    False when the user row does not exist."""
    marked = session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            email=None,
            password_hash=None,
            deleted_at=func.coalesce(User.deleted_at, literal(now)),
        )
    )
    if _rowcount(marked) == 0:
        return False
    session.execute(delete(UserKey).where(UserKey.user_id == user_id))
    _enqueue(session, user_id, now)
    return True


def request_erasure(session: Session, user_id: uuid.UUID) -> bool:
    """The request path (T11.2b.2): one short transaction, committed here. False when the
    user does not exist (nothing changed)."""
    session.execute(text(f"SET LOCAL lock_timeout = '{REQUEST_LOCK_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL statement_timeout = '{REQUEST_STATEMENT_TIMEOUT}'"))
    done = _short_step(session, user_id, dt.datetime.now(dt.UTC))
    session.commit()
    return done


def erase_user(session: Session, user_id: uuid.UUID) -> None:
    """Synchronous hard delete (cascades to key/conversations/messages) + tombstone ``done``.

    Offline tool only (no concurrent traffic: ``key_recovery.sh erase``) — its transaction
    grows with the history. The API uses ``request_erasure`` + the purger."""
    session.execute(delete(User).where(User.id == user_id))
    already = session.scalar(select(DeletionRequest).where(DeletionRequest.user_id == user_id))
    if already is None:
        now = dt.datetime.now(dt.UTC)
        session.add(DeletionRequest(user_id=user_id, status="done", completed_at=now))
    session.commit()


def purge_orphaned(session: Session) -> int:
    """'No key → no data' (ADR phase 6): every user without a data key is ENQUEUED — scrubbed,
    marked deleted and given a ``pending`` tombstone — for the batched purger; nothing is
    bulk-deleted here. Users already marked deleted with an open tombstone are left alone.
    Returns the number enqueued."""
    now = dt.datetime.now(dt.UTC)
    has_key = exists().where(UserKey.user_id == User.id)
    open_tombstone = exists().where(
        DeletionRequest.user_id == User.id, DeletionRequest.status.in_(OPEN_STATUSES)
    )
    user_ids = list(
        session.scalars(
            select(User.id).where(~has_key).where(User.deleted_at.is_(None) | ~open_tombstone)
        )
    )
    for user_id in user_ids:
        _short_step(session, user_id, now)
    session.commit()
    return len(user_ids)


def replay_deletions(session: Session) -> int:
    """After a restore, before reopening (as the OWNER): every tombstoned account the restore
    brought back gets the short step again (key deleted, PII scrubbed, ``deleted_at``) and
    its tombstone re-queued ``pending``; then ``purge_orphaned``. The purger removes the rows
    (``restore.sh`` runs it right after). Returns the number of accounts re-applied."""
    now = dt.datetime.now(dt.UTC)
    user_ids = list(
        session.scalars(
            select(DeletionRequest.user_id).where(
                exists().where(User.id == DeletionRequest.user_id)
            )
        )
    )
    for user_id in user_ids:
        _short_step(session, user_id, now)
    session.commit()
    purge_orphaned(session)
    return len(user_ids)


def main(argv: list[str] | None = None) -> int:
    from rag_app.purger import main as purger_main

    return purger_main(argv)


if __name__ == "__main__":
    sys.exit(main())
