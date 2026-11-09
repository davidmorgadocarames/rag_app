"""Admin MFA end to end (T12.3.4/T12.3.5): enrol -> confirm -> step-up, and the placeholder
``/admin/ping`` (T12.3.2/T12.3.2b) gated by BOTH the permission map and a fresh step-up.

Same cookie/CSRF harness as ``test_auth_cookies.py``/``test_csrf.py``.
"""

from __future__ import annotations

import uuid

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from rag_app.admin import create_admin
from rag_app.api.app import create_app
from rag_app.api.deps import get_signup_tracker
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from rag_app.risk import SignupTracker
from rag_app.sessions import SESSION_COOKIE_NAME

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    return db_engine


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    return TestClient(app, base_url=BASE_URL)


def _csrf_header(response) -> dict[str, str]:  # noqa: ANN001
    token = response.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def _credentials() -> dict[str, str]:
    return {
        "email": f"mfa-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def _login_as_admin(db_engine: Engine, client: TestClient) -> tuple[str, dict[str, str]]:
    """Creates a FRESH admin account (the bootstrap CLI, T12.3.3 -- no API path does this),
    logs in through the normal endpoint and returns (password, csrf headers)."""
    email = f"admin-{uuid.uuid4().hex[:10]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    with Session(db_engine) as session:
        create_admin(session, email, password)
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return password, _csrf_header(login)


def test_enroll_returns_a_secret_and_a_provisioning_uri(api_env: Engine) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    enroll = client.post("/account/mfa/enroll", headers=_csrf_header(register))
    assert enroll.status_code == 200
    body = enroll.json()
    assert len(body["secret"]) >= 16
    assert body["provisioning_uri"].startswith("otpauth://totp/")


def test_confirm_with_a_wrong_code_fails_and_mfa_stays_disabled(api_env: Engine) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    enroll = client.post("/account/mfa/enroll", headers=_csrf_header(register))
    confirm = client.post(
        "/account/mfa/confirm", json={"code": "000000"}, headers=_csrf_header(register)
    )
    assert confirm.status_code == 400
    assert enroll.status_code == 200  # sanity: enrolment itself succeeded


def test_confirm_with_the_right_code_enables_mfa_and_returns_recovery_codes(
    api_env: Engine,
) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    enroll = client.post("/account/mfa/enroll", headers=_csrf_header(register))
    secret = enroll.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = client.post(
        "/account/mfa/confirm", json={"code": code}, headers=_csrf_header(register)
    )
    assert confirm.status_code == 200
    codes = confirm.json()["recovery_codes"]
    assert len(codes) == 10
    assert len({c.replace("-", "") for c in codes}) == 10  # all distinct


def test_step_up_with_a_valid_totp_code_rotates_the_session_and_sets_mfa_verified(
    api_env: Engine,
) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    old_cookie = register.cookies.get(SESSION_COOKIE_NAME)
    enroll = client.post("/account/mfa/enroll", headers=_csrf_header(register))
    secret = enroll.json()["secret"]
    code = pyotp.TOTP(secret).now()
    client.post("/account/mfa/confirm", json={"code": code}, headers=_csrf_header(register))

    step_up = client.post(
        "/account/mfa/step-up",
        json={"code": pyotp.TOTP(secret).now()},
        headers=_csrf_header(register),
    )
    assert step_up.status_code == 200
    new_cookie = step_up.cookies.get(SESSION_COOKIE_NAME)
    assert new_cookie and new_cookie != old_cookie

    # The OLD session id is gone (rotate() deletes it) -- it must not authenticate any more.
    stale = client.get("/auth/me", cookies={SESSION_COOKIE_NAME: old_cookie})
    assert stale.status_code == 401


def test_step_up_with_an_invalid_code_is_rejected(api_env: Engine) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    enroll = client.post("/account/mfa/enroll", headers=_csrf_header(register))
    secret = enroll.json()["secret"]
    client.post(
        "/account/mfa/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers=_csrf_header(register),
    )
    step_up = client.post(
        "/account/mfa/step-up", json={"code": "000000"}, headers=_csrf_header(register)
    )
    assert step_up.status_code == 403


def test_step_up_requires_mfa_to_be_enabled_first(api_env: Engine) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    step_up = client.post(
        "/account/mfa/step-up", json={"code": "123456"}, headers=_csrf_header(register)
    )
    assert step_up.status_code == 400


def test_a_recovery_code_steps_up_and_is_then_single_use(api_env: Engine) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    enroll = client.post("/account/mfa/enroll", headers=_csrf_header(register))
    secret = enroll.json()["secret"]
    confirm = client.post(
        "/account/mfa/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers=_csrf_header(register),
    )
    recovery_code = confirm.json()["recovery_codes"][0]

    first = client.post(
        "/account/mfa/step-up", json={"code": recovery_code}, headers=_csrf_header(register)
    )
    assert first.status_code == 200

    # The session rotated, so the CSRF cookie did too -- re-present it to isolate the
    # recovery code's single-use behaviour from an unrelated CSRF failure.
    second = client.post(
        "/account/mfa/step-up", json={"code": recovery_code}, headers=_csrf_header(first)
    )
    assert second.status_code == 403


# --- /admin/ping: permission map + MFA step-up, end to end ----------------------------------


def test_an_ordinary_user_gets_403_on_admin_ping(api_env: Engine) -> None:
    client = _client()
    client.post("/auth/register", json=_credentials())
    assert client.get("/auth/me").status_code == 200  # sanity: the user IS authenticated
    response = client.get("/admin/ping")
    assert response.status_code == 403


def test_an_admin_without_mfa_stepup_gets_403_on_admin_ping(
    api_env: Engine, db_engine: Engine
) -> None:
    client = _client()
    _login_as_admin(db_engine, client)
    response = client.get("/admin/ping")
    assert response.status_code == 403


def test_a_stepped_up_admin_gets_200_on_admin_ping(api_env: Engine, db_engine: Engine) -> None:
    client = _client()
    _password, csrf = _login_as_admin(db_engine, client)
    enroll = client.post("/account/mfa/enroll", headers=csrf)
    secret = enroll.json()["secret"]
    client.post("/account/mfa/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=csrf)
    step_up = client.post(
        "/account/mfa/step-up", json={"code": pyotp.TOTP(secret).now()}, headers=csrf
    )
    assert step_up.status_code == 200

    response = client.get("/admin/ping")
    assert response.status_code == 200
    assert response.json()["detail"] == "pong"


def test_no_session_at_all_gets_401_on_admin_ping(api_env: Engine) -> None:
    client = _client()
    assert client.get("/admin/ping").status_code == 401
