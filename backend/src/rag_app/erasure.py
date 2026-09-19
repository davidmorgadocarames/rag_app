"""GDPR erasure: hard delete + crypto-shred + tombstone, with a no-key purge invariant.

See docs/adr/0002-data-erasure-gdpr.md. Deleting a user cascades (FK ON DELETE CASCADE)
to their key, conversations and messages — hard-deleting the data and crypto-shredding
in one step. A tombstone is retained so deletions can be replayed after a restore.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CursorResult, Result, delete, exists, select
from sqlalchemy.orm import Session

from rag_app.db.models import DeletionRequest, User, UserKey


def _rowcount(result: Result[Any]) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def erase_user(session: Session, user_id: uuid.UUID) -> None:
    """Hard-delete the user (cascades to key/conversations/messages) and tombstone it."""
    session.execute(delete(User).where(User.id == user_id))
    already = session.scalar(select(DeletionRequest).where(DeletionRequest.user_id == user_id))
    if already is None:
        session.add(DeletionRequest(user_id=user_id))
    session.commit()


def purge_orphaned(session: Session) -> int:
    """Enforce 'no key -> no data': delete any user that has no data key.

    Cascades remove that user's conversations and messages. This makes a missing key
    mechanically imply deleted data, even for rows reintroduced by a backup restore.
    """
    result = session.execute(delete(User).where(~exists().where(UserKey.user_id == User.id)))
    session.commit()
    return _rowcount(result)


def replay_deletions(session: Session) -> int:
    """Re-apply every tombstoned deletion (run after a restore, before reopening)."""
    user_ids = list(session.scalars(select(DeletionRequest.user_id)))
    reapplied = 0
    for user_id in user_ids:
        result = session.execute(delete(User).where(User.id == user_id))
        reapplied += _rowcount(result)
    session.commit()
    purge_orphaned(session)
    return reapplied
