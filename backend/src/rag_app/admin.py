"""Admin bootstrap CLI (T12.3.3): the ONLY code path in the whole application that ever
writes ``role = 'admin'``. No API route can change ``role`` (grep-checked by
``test_authz.py``/``test_client_tampering.py``) -- the permission map
(``rag_app.authz``) only ever READS it.

    python -m rag_app.admin create [--email EMAIL]
    python -m rag_app.admin demo-account [--email EMAIL]

The admin password is always read from an interactive prompt (``getpass``, twice, never
echoed) -- never a command-line argument (would land in shell history and the process
list) and never logged. The email may be passed with ``--email`` (not a secret) or typed
when prompted.

``demo-account`` (T12.4.11, DEC-007) is a thin extension of the same bootstrap for the
interview/demo friction problem (12b requires a verified email + idles out after 30 min):
ONE already-verified ``role='user'`` account, password a strong random value GENERATED
here (never prompted, never an argument, never logged) and printed to stdout exactly once
for the operator to copy -- erase it afterwards through the normal `DELETE /account` path
(or the admin CLI has no special-case deletion of its own). Full demo-account tooling
(self-service, multiple accounts, reset-on-demand) stays in 18.1.2; this is only the
pre-interview "create one" step the 12b runbook procedure drives.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from rag_app.crypto import generate_user_key, wrap_key
from rag_app.db.models import User, UserKey
from rag_app.password_policy import validate_password
from rag_app.security import hash_password

ADMIN_ROLE = "admin"
USER_ROLE = "user"
# A demo account's password is generated, not chosen -- long enough that the NCSC-denylist/
# email-substring checks in validate_password are nothing more than a formality (it is drawn
# fresh from `secrets`, never derived from the email), but still run so this path can never
# silently create an account that would fail the SAME policy a real register call enforces.
DEMO_PASSWORD_BYTES = 18


class AdminCreateError(RuntimeError):
    """A problem creating the admin/demo account. The message never carries the password."""


def create_verified_user(session: Session, email: str, password: str, *, role: str) -> User:
    """Create a new, already-verified account with ``role`` (so it can log in immediately
    without an e-mail round trip). Raises ``AdminCreateError`` if ``email`` is already
    registered. Caller commits (this function commits internally, matching ``register``'s
    own transaction boundary)."""
    existing = session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise AdminCreateError("a user with that email already exists")
    user = User(
        email=email,
        password_hash=hash_password(password),
        email_verified=True,
        role=role,
    )
    session.add(user)
    session.flush()
    session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
    session.commit()
    return user


def create_admin(session: Session, email: str, password: str) -> User:
    """``role='admin'`` (T12.3.3): thin alias of ``create_verified_user``."""
    return create_verified_user(session, email, password, role=ADMIN_ROLE)


def _prompt_password(email: str) -> str:
    """T12.4.3: the SAME password policy as a normal registration (``rag_app.password_policy``,
    checked against the email already entered) -- an admin account is never exempt."""
    while True:
        password = getpass.getpass("Password: ")
        problems = validate_password(password, email)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("passwords do not match, try again", file=sys.stderr)
            continue
        return password


def _generate_demo_password(email: str) -> str:
    """T12.4.11: drawn fresh from ``secrets`` (never derived from ``email``, never a command
    -line argument, never logged) -- validated against the SAME policy a real register call
    enforces (T12.4.3), regenerated in the astronomically unlikely case a random draw
    collides with the NCSC denylist or the account's own email local part."""
    while True:
        password = secrets.token_urlsafe(DEMO_PASSWORD_BYTES)
        if not validate_password(password, email):
            return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create", help="create a new admin account (interactive password)")
    create.add_argument("--email", help="omit to be prompted")
    demo = sub.add_parser(
        "demo-account",
        help="create one verified demo account (role=user, random password shown once; "
        "DEC-007/T12.4.11)",
    )
    demo.add_argument("--email", help="omit to be prompted")
    args = parser.parse_args(argv)

    from rag_app.db.session import make_engine

    if args.cmd == "create":
        email = args.email or input("Email: ").strip()
        password = _prompt_password(email)
        engine = make_engine()
        try:
            with Session(engine) as session:
                try:
                    user = create_admin(session, email, password)
                except AdminCreateError as exc:
                    print(f"admin create: FAIL — {exc}", file=sys.stderr)
                    return 1
                user_id = user.id  # read while still attached, before the session closes
        finally:
            engine.dispose()
        print(f"admin created: {user_id}")  # id only -- never the email (PII) on stdout
        return 0

    if args.cmd == "demo-account":
        email = args.email or input("Email: ").strip()
        password = _generate_demo_password(email)
        engine = make_engine()
        try:
            with Session(engine) as session:
                try:
                    user = create_verified_user(session, email, password, role=USER_ROLE)
                except AdminCreateError as exc:
                    print(f"demo-account: FAIL — {exc}", file=sys.stderr)
                    return 1
                user_id = user.id
        finally:
            engine.dispose()
        print(f"demo account created: {user_id}")  # id only -- never the email (PII)
        print(f"password (shown once, never logged -- copy it now): {password}")
        return 0

    return 1  # pragma: no cover - argparse already rejects anything else


if __name__ == "__main__":
    sys.exit(main())
