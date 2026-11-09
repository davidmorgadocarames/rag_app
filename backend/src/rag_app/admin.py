"""Admin bootstrap CLI (T12.3.3): the ONLY code path in the whole application that ever
writes ``role = 'admin'``. No API route can change ``role`` (grep-checked by
``test_authz.py``/``test_client_tampering.py``) -- the permission map
(``rag_app.authz``) only ever READS it.

    python -m rag_app.admin create [--email EMAIL]

The password is always read from an interactive prompt (``getpass``, twice, never echoed)
-- never a command-line argument (would land in shell history and the process list) and
never logged. The email may be passed with ``--email`` (not a secret) or typed when
prompted.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from rag_app.crypto import generate_user_key, wrap_key
from rag_app.db.models import User, UserKey
from rag_app.security import hash_password

ADMIN_ROLE = "admin"
MIN_PASSWORD_LENGTH = 12  # T12.4.3 brings the full NCSC-list policy; this is a sane floor
# for a bootstrap tool used before that lands.


class AdminCreateError(RuntimeError):
    """A problem creating the admin account. The message never carries the password."""


def create_admin(session: Session, email: str, password: str) -> User:
    """Create a new, already-verified admin account (so it can log in immediately without
    an e-mail round trip). Raises ``AdminCreateError`` if ``email`` is already registered.
    Caller commits (this function commits internally, matching ``register``'s own
    transaction boundary)."""
    existing = session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise AdminCreateError("a user with that email already exists")
    user = User(
        email=email,
        password_hash=hash_password(password),
        email_verified=True,
        role=ADMIN_ROLE,
    )
    session.add(user)
    session.flush()
    session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
    session.commit()
    return user


def _prompt_password() -> str:
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("passwords do not match, try again", file=sys.stderr)
            continue
        return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create", help="create a new admin account (interactive password)")
    create.add_argument("--email", help="omit to be prompted")
    args = parser.parse_args(argv)

    if args.cmd != "create":  # pragma: no cover - argparse already rejects anything else
        return 1

    email = args.email or input("Email: ").strip()
    password = _prompt_password()

    from rag_app.db.session import make_engine

    engine = make_engine()
    try:
        with Session(engine) as session:
            try:
                user = create_admin(session, email, password)
            except AdminCreateError as exc:
                print(f"admin create: FAIL — {exc}", file=sys.stderr)
                return 1
            user_id = user.id  # read while still attached, before the session closes below
    finally:
        engine.dispose()
    print(f"admin created: {user_id}")  # id only -- never the email (PII) on stdout
    return 0


if __name__ == "__main__":
    sys.exit(main())
