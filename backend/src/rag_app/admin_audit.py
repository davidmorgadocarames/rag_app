"""Admin action audit trail (0008, T13.1.6): every mutating ``/admin/*`` route calls
``record_admin_action`` exactly once, in the SAME request/transaction as the action itself.

``target`` is always an OPAQUE identifier (an erasure request id today -- never an email or
any other raw value a client submitted, T13.1.7/T13.1.8) -- see
``rag_app.db.models.AdminAction`` and ``rag_app.erasure.PURGE_STEPS``' own ``admin_actions``
step (nulls ``admin_user_id`` -- never the row itself -- if the acting admin is later erased,
keeping the audit trail intact)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from rag_app.db.models import AdminAction


def record_admin_action(
    session: Session,
    admin_user_id: uuid.UUID,
    action: str,
    *,
    target: str | None = None,
    result: str,
) -> None:
    """Adds the row (caller commits, same convention as ``rag_app.erasure._enqueue``)."""
    session.add(
        AdminAction(admin_user_id=admin_user_id, action=action, target=target, result=result)
    )
