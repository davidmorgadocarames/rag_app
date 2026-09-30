"""Data helper for ``scripts/db/backup_drill.sh`` (gate step ``backup-drill``, T11.2.10).

Runs against a throwaway drill database on the gate server (``DATABASE_URL``, set by the
drill script) — never the development database.

    python -m rag_app.devtools.backup_drill seed            → {"keep": "<id>", "erase": "<id>"}
    python -m rag_app.devtools.backup_drill erase --user ID (the app's erasure path)
    python -m rag_app.devtools.backup_drill state --user ID [--user ID …]
        → {"<id>": {"user": bool, "key": bool, "conversations": n, "messages": n,
                    "tombstone": "<status>" | null}, …}

``seed`` creates two synthetic accounts, each with a data key wrapped under a throwaway master
key (generated in memory and discarded), one conversation and two encrypted messages.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rag_app.db.models import Conversation, DeletionRequest, Message, User, UserKey
from rag_app.erasure import erase_user


def seed(session: Session) -> dict[str, str]:
    master = Fernet(Fernet.generate_key())
    ids: dict[str, str] = {}
    for label in ("keep", "erase"):
        user = User(email=f"drill-{label}-{uuid.uuid4().hex[:8]}@example.test", password_hash="x")
        session.add(user)
        session.flush()
        data_key = Fernet.generate_key()
        session.add(UserKey(user_id=user.id, wrapped_key=master.encrypt(data_key)))
        conversation = Conversation(
            user_id=user.id, title_encrypted=Fernet(data_key).encrypt(b"drill")
        )
        session.add(conversation)
        session.flush()
        for role in ("user", "assistant"):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role=role,
                    content_encrypted=Fernet(data_key).encrypt(f"drill {role}".encode()),
                )
            )
        ids[label] = str(user.id)
    session.commit()
    return ids


def state(session: Session, user_ids: list[uuid.UUID]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for user_id in user_ids:
        conversations = session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
        )
        messages = session.scalar(
            select(func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.user_id == user_id)
        )
        result[str(user_id)] = {
            "user": session.get(User, user_id) is not None,
            "key": session.get(UserKey, user_id) is not None,
            "conversations": conversations or 0,
            "messages": messages or 0,
            "tombstone": session.scalar(
                select(DeletionRequest.status).where(DeletionRequest.user_id == user_id)
            ),
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.devtools.backup_drill")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("erase").add_argument("--user", type=uuid.UUID, required=True)
    sub.add_parser("state").add_argument("--user", type=uuid.UUID, action="append", required=True)
    args = parser.parse_args(argv)

    from rag_app.db.session import make_engine

    engine = make_engine()
    try:
        with Session(engine) as session:
            if args.cmd == "seed":
                print(json.dumps(seed(session)))
            elif args.cmd == "erase":
                erase_user(session, args.user)
                print(json.dumps({"erased": str(args.user)}))
            else:
                print(json.dumps(state(session, args.user), sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
