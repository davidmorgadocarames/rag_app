"""Cookie auth end to end (T12.1.3): cookie and legacy Bearer both authenticate, logout
invalidates the cookie server-side, erasure revokes every session, and register/login still
return the JWT (rollback path, TF10) while also setting the `__Host-` cookie.

An ``https://`` base_url is required: httpx/TestClient drops a ``Secure`` cookie received over
plain ``http://`` (B7), and ``__Host-`` always implies ``Secure``.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rag_app.api.app import create_app
from rag_app.api.deps import get_signup_tracker
from rag_app.risk import SignupTracker
from rag_app.security import decode_token
from rag_app.sessions import SESSION_COOKIE_NAME

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
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
    return TestClient(app, base_url=BASE_URL)


def _credentials() -> dict[str, str]:
    return {
        "email": f"cookie-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def test_register_sets_a_host_prefixed_secure_cookie_and_still_returns_the_jwt(
    api_env: Engine,
) -> None:
    client = _client()
    response = client.post("/auth/register", json=_credentials())
    assert response.status_code == 201
    assert response.json()["access_token"]  # rollback path, TF10

    raw_cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert raw_cookie
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME.startswith("__Host-")
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert attribute.lower() in set_cookie.lower(), set_cookie


def test_login_also_sets_a_session_cookie(api_env: Engine) -> None:
    client = _client()
    creds = _credentials()
    client.post("/auth/register", json=creds)
    response = client.post("/auth/login", json=creds)
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.cookies.get(SESSION_COOKIE_NAME)


def test_cookie_and_legacy_bearer_both_authenticate(api_env: Engine) -> None:
    client = _client()
    creds = _credentials()
    register = client.post("/auth/register", json=creds)
    access_token = register.json()["access_token"]

    # The cookie path: the client's jar already holds it from the register response.
    via_cookie = client.get("/auth/me")
    assert via_cookie.status_code == 200
    assert via_cookie.json()["email"] == creds["email"]

    # The legacy Bearer path, isolated from the cookie by using a client with no jar entry.
    bearer_client = _client()
    via_bearer = bearer_client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert via_bearer.status_code == 200
    assert via_bearer.json()["email"] == creds["email"]


def test_an_unknown_cookie_is_rejected_even_with_a_valid_bearer_present(api_env: Engine) -> None:
    """get_current_user tries the cookie first and does not fall back to Bearer when the
    cookie is present but invalid (see its docstring)."""
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    access_token = register.json()["access_token"]

    response = client.get(
        "/auth/me",
        cookies={SESSION_COOKIE_NAME: "garbage-not-a-real-session"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 401


def test_logout_makes_the_cookie_fail_server_side(api_env: Engine) -> None:
    client = _client()
    register = client.post("/auth/register", json=_credentials())
    raw_cookie = register.cookies.get(SESSION_COOKIE_NAME)
    assert raw_cookie

    assert client.get("/auth/me").status_code == 200

    logout = client.post("/auth/logout")
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
    creds = _credentials()
    register = client.post("/auth/register", json=creds)
    access_token = register.json()["access_token"]
    first_cookie = register.cookies.get(SESSION_COOKIE_NAME)
    assert first_cookie
    user_id = decode_token(access_token)
    assert user_id

    # A second session for the SAME user (another device/browser): erasure must revoke it
    # too, not only the one whose cookie happened to make the DELETE request.
    second_login = client.post("/auth/login", json=creds)
    second_cookie = second_login.cookies.get(SESSION_COOKIE_NAME)
    assert second_login.status_code == 200 and second_cookie and second_cookie != first_cookie

    erase = client.delete("/account", headers={"Authorization": f"Bearer {access_token}"})
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
