"""rag_app.admin (T12.3.3): the ONLY code path that ever writes ``role = 'admin'`` -- an
interactive password (never a CLI argument, never logged)."""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from rag_app.admin import AdminCreateError, create_admin, create_verified_user, main
from rag_app.db.models import User, UserKey
from rag_app.erasure import request_erasure

pytestmark = [pytest.mark.db, pytest.mark.security]


@pytest.fixture()
def master_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATA_MASTER_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    return key


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:10]}@example.test"


def test_create_admin_sets_role_admin_verified_and_a_data_key(
    db_engine: Engine, master_key: str
) -> None:
    email = _email()
    with Session(db_engine) as session:
        user = create_admin(session, email, "correct horse battery staple")
        assert user.role == "admin"
        assert user.email_verified is True

    with Session(db_engine) as session:
        row = session.scalar(select(User).where(User.id == user.id))
        assert row is not None and row.role == "admin" and row.email_verified
        key = session.scalar(select(UserKey).where(UserKey.user_id == user.id))
        assert key is not None  # crypto-shred erasure needs a data key, same as register


def test_create_admin_refuses_a_duplicate_email(db_engine: Engine, master_key: str) -> None:
    email = _email()
    with Session(db_engine) as session:
        create_admin(session, email, "correct horse battery staple")
    with Session(db_engine) as session, pytest.raises(AdminCreateError):
        create_admin(session, email, "a different password entirely")


def test_no_api_route_ever_sets_role() -> None:
    """Grep-level proof (T12.3.2b/T12.3.3): no request schema has a ``role`` field, so no
    endpoint can accept it from the client even if a handler forgot to ignore it."""
    from rag_app.api import schemas

    for name in dir(schemas):
        obj = getattr(schemas, name)
        if isinstance(obj, type) and issubclass(obj, schemas.StrictModel):
            assert "role" not in obj.model_fields, name


def test_the_cli_never_prints_the_password(
    db_engine: Engine,
    master_key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``DATABASE_URL`` is already the harness database for the duration of this ``db``
    test (the autouse fixture in ``conftest.py``), so ``main()``'s own ``make_engine()``
    (no explicit URL) resolves it exactly like the real CLI would."""
    email = _email()
    password = "correct horse battery staple 2026"
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": password)

    assert main(["create", "--email", email]) == 0
    out = capsys.readouterr().out
    assert password not in out
    assert email not in out  # id only -- not even the e-mail (PII) on stdout

    with Session(db_engine) as session:
        row = session.scalar(select(User).where(User.email == email))
        assert row is not None and row.role == "admin"


# --- demo-account (T12.4.11, DEC-007) ------------------------------------------------------


def test_create_verified_user_with_role_user_is_a_normal_verified_account(
    db_engine: Engine, master_key: str
) -> None:
    email = _email()
    with Session(db_engine) as session:
        user = create_verified_user(session, email, "a-strong-random-password-1234", role="user")
        assert user.role == "user"
        assert user.email_verified is True

    with Session(db_engine) as session:
        row = session.scalar(select(User).where(User.id == user.id))
        assert row is not None and row.role == "user" and row.email_verified
        key = session.scalar(select(UserKey).where(UserKey.user_id == user.id))
        assert key is not None


def test_demo_account_cli_shows_the_generated_password_exactly_once_never_logged(
    db_engine: Engine, master_key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """T12.4.11: unlike `create` (interactive, never shown), `demo-account` GENERATES the
    password and must print it exactly once on stdout -- the only place the operator can
    read it from -- while still never printing the email (PII)."""
    email = _email()
    assert main(["demo-account", "--email", email]) == 0
    out = capsys.readouterr().out
    assert email not in out
    assert "password (shown once" in out

    with Session(db_engine) as session:
        row = session.scalar(select(User).where(User.email == email))
        assert row is not None and row.role == "user" and row.email_verified is True
        user_id = row.id

    # Erasable through the normal path (D-ER) -- no special-case deletion for demo accounts.
    with Session(db_engine) as session:
        assert request_erasure(session, user_id) is True
        row = session.scalar(select(User).where(User.id == user_id))
        assert row is not None and row.deleted_at is not None and row.email is None


def test_demo_account_refuses_a_duplicate_email(db_engine: Engine, master_key: str) -> None:
    email = _email()
    with Session(db_engine) as session:
        create_verified_user(session, email, "a-strong-random-password-1234", role="user")
    with Session(db_engine) as session, pytest.raises(AdminCreateError):
        create_verified_user(session, email, "a-different-password-entirely", role="user")
