"""Password reset (0007 "part 3", T12.4.6) + the DA re-check narrowing of register's
``except IntegrityError``.

Same REC-04/REC-05 pattern (docs/security/THREAT_MODEL.md "X10"): a genuine
thread-concurrency test PLUS a deterministic structural companion for each atomic
token-consume function, because a race condition is often only probabilistically
reproduced by a threaded test alone.
"""

from __future__ import annotations

import datetime as dt
import inspect
import logging
import threading
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from rag_app.api import auth
from rag_app.api.app import create_app
from rag_app.api.auth import (
    _consume_email_verification_token,
    _consume_password_reset_token,
)
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.db.models import EmailVerificationToken, PasswordResetToken, User
from rag_app.email_abuse import EmailAbuseLimiter
from rag_app.lockout import lockout_key
from rag_app.risk import SignupTracker
from rag_app.security import (
    generate_verification_token,
    hash_password,
    hash_token,
    verify_password,
)
from rag_app.sessions import SESSION_COOKIE_NAME

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"
LOCKOUT_SECRET = "l" * 48


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("LOCKOUT_HMAC_SECRET", LOCKOUT_SECRET)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    return db_engine


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    app.dependency_overrides[get_email_abuse_limiter] = EmailAbuseLimiter
    return TestClient(app, base_url=BASE_URL, raise_server_exceptions=raise_server_exceptions)


def _password() -> str:
    return "correct horse battery staple " + uuid.uuid4().hex[:6]


def _new_email() -> str:
    return f"reset-{uuid.uuid4().hex[:12]}@example.test"


def _register(client: TestClient) -> tuple[str, str]:
    """Registers a fresh account; returns (email, password). Not yet verified -- callers
    that need a verified account also call ``_verify_via_db``."""
    email, password = _new_email(), _password()
    register = client.post("/auth/register", json={"email": email, "password": password})
    assert register.status_code == 202, register.text
    return email, password


def _verify_via_db(db_engine: Engine, email: str) -> None:
    with Session(db_engine) as session:
        token_row = session.scalar(
            select(EmailVerificationToken)
            .join(User, User.id == EmailVerificationToken.user_id)
            .where(User.email == email)
        )
        assert token_row is not None
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified = True
        session.commit()


# --- DA re-check: register's except IntegrityError only treats the email conflict as such --


def test_register_reraises_an_integrity_error_that_is_not_the_email_conflict(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T12.4.6 DA re-check: a NOT NULL violation on ``user_keys.wrapped_key`` (forced here by
    making ``wrap_key`` return ``None``) is NOT the ``users.email`` unique-constraint
    conflict -- it must surface as a 500, never the enumeration-safe generic 202, and the
    log line must carry no PII (never the submitted email)."""
    monkeypatch.setattr(auth, "wrap_key", lambda _key: None)
    client = _client(raise_server_exceptions=False)
    email = _new_email()
    with caplog.at_level(logging.ERROR):
        response = client.post("/auth/register", json={"email": email, "password": _password()})
    assert response.status_code == 500
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert email not in text
    assert "@example.test" not in text
    # Sanity: no row was left half-created (the whole transaction rolled back).
    with Session(api_env) as session:
        assert session.scalar(select(User).where(User.email == email)) is None


def test_register_still_returns_the_generic_202_for_a_real_email_conflict(
    api_env: Engine,
) -> None:
    """Companion sanity check: the narrowing above must not have broken the ORIGINAL
    (correct) branch -- a genuine duplicate-email registration still gets the SAME generic
    202 it always did (DA-12bB-1)."""
    client = _client()
    creds = {"email": _new_email(), "password": _password()}
    first = client.post("/auth/register", json=creds)
    assert first.status_code == 202
    second = client.post("/auth/register", json=creds)
    assert second.status_code == 202
    assert second.json()["detail"] == first.json()["detail"]


# --- X10: email verification token, atomic consume ------------------------------------------


def test_concurrent_verifications_with_the_same_token_let_exactly_one_through(
    db_engine: Engine,
) -> None:
    raw_token = "concurrent-verify-" + uuid.uuid4().hex
    with Session(db_engine) as session:
        user = User(
            email=f"verify-race-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password(_password()),
        )
        session.add(user)
        session.flush()
        session.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
            )
        )
        session.commit()

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[uuid.UUID | None] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        with Session(db_engine) as session:
            claimed = _consume_email_verification_token(session, raw_token, dt.datetime.now(dt.UTC))
            session.commit()
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=claim) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == thread_count
    assert (
        sum(1 for r in results if r is not None) == 1
    ), "more than one thread claimed the same email verification token"


def test_consume_email_verification_token_is_a_single_atomic_statement() -> None:
    source = inspect.getsource(_consume_email_verification_token)
    assert source.count("session.execute(") == 1, (
        "_consume_email_verification_token must issue exactly one DB statement (a single "
        "atomic UPDATE), not a SELECT followed by a separate UPDATE"
    )
    assert "update(EmailVerificationToken)" in source
    assert "EmailVerificationToken.used_at.is_(None)" in source


# --- X10: password reset token, atomic consume -----------------------------------------------


def test_concurrent_resets_with_the_same_token_let_exactly_one_through(db_engine: Engine) -> None:
    raw_token = "concurrent-reset-" + uuid.uuid4().hex
    with Session(db_engine) as session:
        user = User(
            email=f"reset-race-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password(_password()),
            email_verified=True,
        )
        session.add(user)
        session.flush()
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30),
            )
        )
        session.commit()

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[uuid.UUID | None] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        with Session(db_engine) as session:
            claimed = _consume_password_reset_token(session, raw_token, dt.datetime.now(dt.UTC))
            session.commit()
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=claim) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == thread_count
    assert (
        sum(1 for r in results if r is not None) == 1
    ), "more than one thread claimed the same password reset token"


def test_consume_password_reset_token_is_a_single_atomic_statement() -> None:
    source = inspect.getsource(_consume_password_reset_token)
    assert source.count("session.execute(") == 1, (
        "_consume_password_reset_token must issue exactly one DB statement (a single atomic "
        "UPDATE), not a SELECT followed by a separate UPDATE"
    )
    assert "update(PasswordResetToken)" in source
    assert "PasswordResetToken.used_at.is_(None)" in source


# --- forgot-password: enumeration-safe response + timing parity -----------------------------


def test_forgot_password_generic_response_whatever_the_outcome(api_env: Engine) -> None:
    client = _client()
    verified_email, _ = _register(client)
    _verify_via_db(api_env, verified_email)

    known = client.post("/auth/forgot-password", json={"email": verified_email})
    unknown = client.post("/auth/forgot-password", json={"email": _new_email()})
    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json()["detail"] == unknown.json()["detail"]


def test_forgot_password_timing_does_not_distinguish_an_existing_account_from_an_unknown_one(
    api_env: Engine,
) -> None:
    """T12.4.6 (changes-since-plan item 2): forgot-password must cost roughly the same
    whether the address has a live, verified account or not -- both branches always do one
    SELECT and send exactly one email via the same ``_send`` call; the real-account branch
    additionally does one small INSERT+commit, well inside the tolerance used elsewhere in
    this suite (login/register timing tests)."""
    import time

    client = _client()
    verified_email, _ = _register(client)
    _verify_via_db(api_env, verified_email)

    repeats = 6

    def _measure_known() -> float:
        start = time.perf_counter()
        for _ in range(repeats):
            response = client.post("/auth/forgot-password", json={"email": verified_email})
            assert response.status_code == 200
        return (time.perf_counter() - start) / repeats

    def _measure_unknown() -> float:
        start = time.perf_counter()
        for _ in range(repeats):
            response = client.post("/auth/forgot-password", json={"email": _new_email()})
            assert response.status_code == 200
        return (time.perf_counter() - start) / repeats

    # Warm-up (first call sometimes pays a one-off cost).
    client.post("/auth/forgot-password", json={"email": _new_email()})
    client.post("/auth/forgot-password", json={"email": verified_email})

    known_avg = _measure_known()
    unknown_avg = _measure_unknown()
    ratio = max(known_avg, unknown_avg) / min(known_avg, unknown_avg)
    assert ratio < 2.0, (
        f"timing differs too much between a known account ({known_avg * 1000:.1f} ms/call) "
        f"and an unknown one ({unknown_avg * 1000:.1f} ms/call)"
    )


# --- reset_password: success path + rejections -----------------------------------------------


def _issue_reset_token(db_engine: Engine, email: str) -> str:
    raw_token = generate_verification_token()
    with Session(db_engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30),
            )
        )
        session.commit()
    return raw_token


def test_reset_password_success_revokes_sessions_and_clears_the_lockout(
    api_env: Engine,
) -> None:
    client = _client()
    email, old_password = _register(client)
    _verify_via_db(api_env, email)

    login = client.post("/auth/login", json={"email": email, "password": old_password})
    assert login.status_code == 200
    assert login.cookies.get(SESSION_COOKIE_NAME)

    # A failed login first, to prove reset also clears the lockout counter.
    client.post("/auth/login", json={"email": email, "password": "definitely-wrong-one"})

    raw_token = _issue_reset_token(api_env, email)
    new_password = _password()
    reset = client.post("/auth/reset", json={"token": raw_token, "password": new_password})
    assert reset.status_code == 200, reset.text

    with Session(api_env) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        assert verify_password(new_password, user.password_hash or "")
        assert not verify_password(old_password, user.password_hash or "")

        from rag_app.db.models import LoginLockout, UserSession

        assert session.scalar(select(UserSession).where(UserSession.user_id == user.id)) is None
        # Scoped to THIS test's own email_hmac -- the harness database is shared across the
        # whole module (session-scoped db_url fixture), so a bare "any row" query would also
        # see other tests' lockout rows.
        key = lockout_key(LOCKOUT_SECRET, email)
        assert session.scalar(select(LoginLockout).where(LoginLockout.email_hmac == key)) is None

    # The old password no longer works; the new one does.
    old_relog = client.post("/auth/login", json={"email": email, "password": old_password})
    assert old_relog.status_code == 401
    relog = client.post("/auth/login", json={"email": email, "password": new_password})
    assert relog.status_code == 200


def test_reset_password_token_is_single_use(api_env: Engine) -> None:
    client = _client()
    email, _ = _register(client)
    _verify_via_db(api_env, email)
    raw_token = _issue_reset_token(api_env, email)

    first = client.post("/auth/reset", json={"token": raw_token, "password": _password()})
    assert first.status_code == 200
    second = client.post("/auth/reset", json={"token": raw_token, "password": _password()})
    assert second.status_code == 400


def test_reset_password_rejects_an_expired_token(api_env: Engine) -> None:
    client = _client()
    email, _ = _register(client)
    _verify_via_db(api_env, email)

    raw_token = generate_verification_token()
    with Session(api_env) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),  # already expired
            )
        )
        session.commit()

    response = client.post("/auth/reset", json={"token": raw_token, "password": _password()})
    assert response.status_code == 400


def test_reset_password_rejects_an_unknown_token(api_env: Engine) -> None:
    client = _client()
    response = client.post(
        "/auth/reset", json={"token": "not-a-real-token", "password": _password()}
    )
    assert response.status_code == 400


def test_reset_password_enforces_the_password_policy_against_the_real_email(
    api_env: Engine,
) -> None:
    """T12.4.3's "must not contain the email local part" check cannot run at the schema
    layer (the token is not yet resolved to a user there) -- the route re-checks it against
    the REAL owner's email before consuming the token."""
    client = _client()
    email, _ = _register(client)
    _verify_via_db(api_env, email)
    raw_token = _issue_reset_token(api_env, email)
    local_part = email.split("@", 1)[0]

    response = client.post(
        "/auth/reset", json={"token": raw_token, "password": f"{local_part}-password-12345"}
    )
    assert response.status_code == 400

    # The token must NOT have been burned by the rejected attempt -- a real reset still works.
    retry = client.post("/auth/reset", json={"token": raw_token, "password": _password()})
    assert retry.status_code == 200, retry.text


def test_reset_password_mass_assignment_is_rejected(api_env: Engine) -> None:
    client = _client()
    response = client.post(
        "/auth/reset",
        json={"token": "whatever", "password": _password(), "role": "admin"},
    )
    assert response.status_code == 422


def test_forgot_password_mass_assignment_is_rejected(api_env: Engine) -> None:
    client = _client()
    response = client.post("/auth/forgot-password", json={"email": _new_email(), "role": "admin"})
    assert response.status_code == 422
