"""Cookie auth end to end (T12.1.3, T12.5.3): the ``__Host-`` session cookie is the ONLY way
to authenticate -- the legacy Bearer JWT path (the pre-BFF frontend's 12a rollback, TF10) was
removed in T12.5.3, along with returning a token from ``/auth/login`` at all. Logout
invalidates the cookie server-side, erasure revokes every session. Register itself no longer
opens a session (T12.4.4) -- see `test_register_no_longer_sets_a_session_or_returns_a_token`
and `_register_and_login` below.

An ``https://`` base_url is required: httpx/TestClient drops a ``Secure`` cookie received over
plain ``http://`` (B7), and ``__Host-`` always implies ``Secure``.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from rag_app.api.app import create_app
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from rag_app.db.models import User
from rag_app.email_abuse import EmailAbuseLimiter
from rag_app.risk import SignupTracker
from rag_app.sessions import SESSION_COOKIE_NAME

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


def _csrf_header(response) -> dict[str, str]:  # noqa: ANN001 - httpx.Response, kept untyped to dodge the import
    """T12.1.4 (12a-C): every cookie-authenticated mutating request in this file now also
    needs a matching X-CSRF-Token -- CSRF itself is tested in test_csrf.py; here it is just
    plumbing so the T12.1.3 cookie-auth behaviour this file actually tests keeps working."""
    token = response.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    return db_engine


def _client() -> TestClient:
    """A fresh app with its OWN signup-risk tracker: ``get_signup_tracker``'s default is a
    single process-wide singleton (``rag_app.api.deps._signup_tracker``), so this whole
    module calling ``/auth/register`` several times would otherwise trip the per-IP velocity
    block (``rag_app.risk``, >= 5 signups/hour from the same TestClient "IP")."""
    app = create_app()
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    # T12.4.8: same reasoning as the signup tracker above -- a fresh EmailAbuseLimiter per
    # app, never the process-wide singleton, so repeated register/resend/forgot calls in
    # this file never trip the new per-IP/global email-abuse limits.
    app.dependency_overrides[get_email_abuse_limiter] = EmailAbuseLimiter
    return TestClient(app, base_url=BASE_URL)


def _credentials() -> dict[str, str]:
    return {
        "email": f"cookie-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def _register_and_login(client: TestClient, creds: dict[str, str] | None = None):  # noqa: ANN201
    """T12.4.4: register no longer opens a session -- every test below that just wants an
    AUTHENTICATED client (as opposed to testing register's own contract) registers, then
    logs in, and uses the LOGIN response's cookie/token. Returns (creds, login_response)."""
    creds = creds or _credentials()
    register = client.post("/auth/register", json=creds)
    assert register.status_code == 202, register.text
    login = client.post("/auth/login", json=creds)
    assert login.status_code == 200, login.text
    return creds, login


def test_register_no_longer_sets_a_session_or_returns_a_token(api_env: Engine) -> None:
    """T12.4.4: a generic 202, no `access_token`, no session cookie -- an account must be
    verified by email before it can do anything, so there is nothing to log in as yet."""
    client = _client()
    response = client.post("/auth/register", json=_credentials())
    assert response.status_code == 202
    assert "access_token" not in response.json()
    assert response.cookies.get(SESSION_COOKIE_NAME) is None


def test_login_also_sets_a_session_cookie(api_env: Engine) -> None:
    client = _client()
    creds = _credentials()
    client.post("/auth/register", json=creds)
    response = client.post("/auth/login", json=creds)
    assert response.status_code == 200
    assert "access_token" not in response.json()
    assert response.cookies.get(SESSION_COOKIE_NAME)


def test_the_cookie_authenticates(api_env: Engine) -> None:
    client = _client()
    creds, _login = _register_and_login(client)

    # The client's jar already holds the cookie from the login response.
    via_cookie = client.get("/auth/me")
    assert via_cookie.status_code == 200
    assert via_cookie.json()["email"] == creds["email"]


def test_a_request_with_no_session_cookie_at_all_is_rejected(api_env: Engine) -> None:
    """T12.5.3: the legacy Bearer path is gone -- there is no fallback any more when the
    cookie is absent (contrast with `test_an_unknown_cookie_is_rejected` below, where a
    cookie IS present but does not resolve)."""
    client = _client()
    _register_and_login(client)
    fresh_client = _client()  # a client with no jar entry at all
    response = fresh_client.get("/auth/me")
    assert response.status_code == 401


def test_an_unknown_cookie_is_rejected(api_env: Engine) -> None:
    client = _client()
    _register_and_login(client)

    response = client.get(
        "/auth/me",
        cookies={SESSION_COOKIE_NAME: "garbage-not-a-real-session"},
    )
    assert response.status_code == 401


def test_logout_makes_the_cookie_fail_server_side(api_env: Engine) -> None:
    client = _client()
    _, login = _register_and_login(client)
    raw_cookie = login.cookies.get(SESSION_COOKIE_NAME)
    assert raw_cookie

    assert client.get("/auth/me").status_code == 200

    logout = client.post("/auth/logout", headers=_csrf_header(login))
    assert logout.status_code == 200

    # Re-present the EXACT old cookie value directly (bypassing whatever the client's own
    # jar did with the Set-Cookie it just received) — this is the server-side guarantee.
    stale = client.get("/auth/me", cookies={SESSION_COOKIE_NAME: raw_cookie})
    assert stale.status_code == 401


def test_logout_without_a_cookie_is_a_no_op_not_an_error(api_env: Engine) -> None:
    client = _client()
    response = client.post("/auth/logout")
    assert response.status_code == 200


def test_erasure_revokes_every_session(api_env: Engine) -> None:
    client = _client()
    creds, login = _register_and_login(client)
    first_cookie = login.cookies.get(SESSION_COOKIE_NAME)
    assert first_cookie
    # T12.5.3: no token to decode the user id from any more -- look it up the same way the
    # backend itself does, from the database.
    with Session(api_env) as session:
        user_id = session.scalar(select(User.id).where(User.email == creds["email"]))
    assert user_id

    # A second session for the SAME user (another device/browser): erasure must revoke it
    # too, not only the one whose cookie happened to make the DELETE request.
    second_login = client.post("/auth/login", json=creds)
    second_cookie = second_login.cookies.get(SESSION_COOKIE_NAME)
    assert second_login.status_code == 200 and second_cookie and second_cookie != first_cookie

    # The client's cookie jar now carries the SECOND session's cookie (login overwrote the
    # first one), so this cookie-authenticated DELETE needs ITS CSRF token, not the first's.
    erase = client.delete("/account", headers=_csrf_header(second_login))
    assert erase.status_code == 202

    for stale_cookie in (first_cookie, second_cookie):
        stale = client.get("/auth/me", cookies={SESSION_COOKIE_NAME: stale_cookie})
        assert stale.status_code == 401

    # Scoped to this test's own user: the harness database is shared across the whole
    # module (session-scoped fixture), so a bare `count(*)` would also see other tests'
    # still-live sessions.
    with api_env.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM sessions WHERE user_id = :uid"), {"uid": user_id}
        ).scalar_one()
    assert remaining == 0
