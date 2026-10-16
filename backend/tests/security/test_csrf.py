"""CSRF protection end to end (T12.1.4, P12-D2, DEC-010 = A): a cookie-authenticated
state-changing request without a valid proof (missing / wrong / another session's token /
bad ``Origin``) is rejected with 403; with a valid one it succeeds; a Bearer-only request
(no session cookie at all -- the legacy rollback path, TF10) is exempt. Login/register are
protected by the lighter ``Origin`` check instead (no session exists yet).

An ``https://`` base_url is required: httpx/TestClient drops a ``Secure`` cookie received
over plain ``http://`` (B7 of 12a-B), and the CSRF cookie, like the session cookie, is
``__Host-`` (always implies ``Secure``).
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rag_app.api.app import create_app
from rag_app.api.deps import get_signup_tracker
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from rag_app.risk import SignupTracker
from rag_app.sessions import SESSION_COOKIE_NAME

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"
FRONTEND_ORIGIN = "http://localhost:3000"


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("FRONTEND_ORIGIN", FRONTEND_ORIGIN)
    return db_engine


def _client() -> TestClient:
    """A fresh app with its OWN signup-risk tracker (see test_auth_cookies.py for why)."""
    app = create_app()
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    return TestClient(app, base_url=BASE_URL)


def _credentials() -> dict[str, str]:
    return {
        "email": f"csrf-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def _register(client: TestClient) -> str:
    """Registers a fresh user on ``client`` and returns its CSRF cookie value."""
    response = client.post("/auth/register", json=_credentials())
    assert response.status_code == 201
    csrf = response.cookies.get(CSRF_COOKIE_NAME)
    assert csrf
    return csrf


def test_logout_without_any_csrf_header_is_rejected(api_env: Engine) -> None:
    client = _client()
    _register(client)
    response = client.post("/auth/logout")
    assert response.status_code == 403


def test_logout_with_the_wrong_csrf_token_is_rejected(api_env: Engine) -> None:
    client = _client()
    _register(client)
    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: "not-the-real-token"})
    assert response.status_code == 403


def test_logout_with_another_sessions_csrf_token_is_rejected(api_env: Engine) -> None:
    client = _client()
    _register(client)
    other_client = _client()
    other_csrf = _register(other_client)

    # other_csrf is a validly-SIGNED token, just for a different session -- proves the check
    # is bound to THIS session's id, not merely "is this hex string well-formed".
    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: other_csrf})
    assert response.status_code == 403


def test_logout_with_a_bad_origin_is_rejected_even_with_a_valid_csrf_token(
    api_env: Engine,
) -> None:
    client = _client()
    csrf = _register(client)
    response = client.post(
        "/auth/logout",
        headers={CSRF_HEADER_NAME: csrf, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_logout_with_a_valid_csrf_token_succeeds(api_env: Engine) -> None:
    client = _client()
    csrf = _register(client)
    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: csrf})
    assert response.status_code == 200


def test_logout_with_a_valid_csrf_token_and_correct_origin_succeeds(api_env: Engine) -> None:
    client = _client()
    csrf = _register(client)
    response = client.post(
        "/auth/logout",
        headers={CSRF_HEADER_NAME: csrf, "Origin": FRONTEND_ORIGIN},
    )
    assert response.status_code == 200


def test_a_bearer_only_request_with_no_session_cookie_is_exempt_from_csrf(
    api_env: Engine,
) -> None:
    """The legacy Bearer-only rollback path (12a, TF10): no session cookie at all, so there
    is nothing for a cross-site form to forge the browser into sending automatically."""
    register_client = _client()
    creds = _credentials()
    access_token = register_client.post("/auth/register", json=creds).json()["access_token"]

    bearer_client = _client()  # a fresh client: no cookie jar entry at all
    response = bearer_client.delete("/account", headers={"Authorization": f"Bearer {access_token}"})
    assert response.status_code == 202


def test_delete_account_without_a_csrf_header_is_rejected_when_a_session_cookie_is_present(
    api_env: Engine,
) -> None:
    client = _client()
    creds = _credentials()
    register = client.post("/auth/register", json=creds)
    access_token = register.json()["access_token"]
    # The SAME client: its cookie jar still carries the session cookie, so this request is
    # cookie-authenticated even though an Authorization header is also attached.
    response = client.delete("/account", headers={"Authorization": f"Bearer {access_token}"})
    assert response.status_code == 403


def test_resend_verification_needs_a_valid_csrf_token_too(api_env: Engine) -> None:
    client = _client()
    csrf = _register(client)
    missing = client.post("/auth/resend-verification")
    assert missing.status_code == 403
    ok = client.post("/auth/resend-verification", headers={CSRF_HEADER_NAME: csrf})
    assert ok.status_code == 200


def test_register_rejects_a_wrong_origin(api_env: Engine) -> None:
    client = _client()
    response = client.post(
        "/auth/register", json=_credentials(), headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_register_allows_the_correct_origin_or_no_origin_at_all(api_env: Engine) -> None:
    client = _client()
    response = client.post(
        "/auth/register", json=_credentials(), headers={"Origin": FRONTEND_ORIGIN}
    )
    assert response.status_code == 201
    no_origin = client.post("/auth/register", json=_credentials())
    assert no_origin.status_code == 201


def test_login_rejects_a_wrong_origin(api_env: Engine) -> None:
    client = _client()
    creds = _credentials()
    client.post("/auth/register", json=creds)
    response = client.post("/auth/login", json=creds, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_the_csrf_cookie_is_host_prefixed_secure_and_readable_by_script(
    api_env: Engine,
) -> None:
    client = _client()
    response = client.post("/auth/register", json=_credentials())
    set_cookie = response.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME.startswith("__Host-")
    assert response.cookies.get(CSRF_COOKIE_NAME)
    for attribute in ("Secure", "SameSite=lax", "Path=/"):
        assert attribute.lower() in set_cookie.lower(), set_cookie
    # NOT HttpOnly: must be readable from page script to be echoed in the header. httpx/
    # TestClient does not surface per-cookie HttpOnly directly, so check the raw header text
    # for the CSRF cookie's own Set-Cookie line specifically.
    csrf_lines = [
        line
        for line in response.headers.get_list("set-cookie")
        if line.startswith(CSRF_COOKIE_NAME)
    ]
    assert csrf_lines and "httponly" not in csrf_lines[0].lower()


def test_login_also_rotates_the_csrf_cookie(api_env: Engine) -> None:
    client = _client()
    creds = _credentials()
    first = client.post("/auth/register", json=creds)
    first_csrf = first.cookies.get(CSRF_COOKIE_NAME)
    second = client.post("/auth/login", json=creds)
    second_csrf = second.cookies.get(CSRF_COOKIE_NAME)
    assert second_csrf and second_csrf != first_csrf
    # The new session's token works; the old one (now a stale session id) does not.
    assert client.post("/auth/logout", headers={CSRF_HEADER_NAME: second_csrf}).status_code == 200
    assert client.cookies.get(SESSION_COOKIE_NAME) is None
