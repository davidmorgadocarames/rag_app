""" "Become admin from the browser or a terminal" (T12.3.6, user idea 2026-11-09): a normal
user, using only what DevTools or ``curl`` allow, never gets admin. Threat catalogue: OWASP
A01 (Broken Access Control) / API Top 10 API5 (Broken Function Level Authorization).

Cases (1)-(6) from the task row, plus a role x route matrix generated from
``rag_app.authz.PROTECTED_ROUTES`` (populated by the actual ``require_permission``/
``require_permission_and_mfa`` calls next to each route -- never a hand-maintained list a
new admin route could silently fall outside of).

Case (5) ("the frontend never decides authorisation") and the Playwright half of case (6)
are frontend/e2e concerns -- out of scope for a backend test; see
``frontend/e2e/admin.spec.ts`` (code written in 12b-A, run in Phase 2 -- PC constraint).
Cases (1)-(4) and (6) are fully provable from the backend alone, which is the actual
authority (DA-G... style: the backend must refuse even if a hypothetical compromised/
bypassed frontend sent exactly the same requests)."""

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
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.authz import PROTECTED_ROUTES, ROLE_PERMISSIONS
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from rag_app.email_abuse import EmailAbuseLimiter
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
    app.dependency_overrides[get_email_abuse_limiter] = EmailAbuseLimiter
    return TestClient(app, base_url=BASE_URL)


def _csrf_header(response) -> dict[str, str]:  # noqa: ANN001
    token = response.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def _credentials() -> dict[str, str]:
    return {
        "email": f"tamper-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def _register_and_login(client: TestClient):  # noqa: ANN201
    """T12.4.4: register no longer opens a session -- every test below that just wants an
    AUTHENTICATED ordinary-user client registers, then logs in, and uses the LOGIN
    response (not register's)."""
    creds = _credentials()
    register = client.post("/auth/register", json=creds)
    assert register.status_code == 202, register.text
    login = client.post("/auth/login", json=creds)
    assert login.status_code == 200, login.text
    return login


def _stepped_up_admin(db_engine: Engine, client: TestClient) -> dict[str, str]:
    """A fresh admin account, logged in AND MFA-stepped-up -- the only way this backend ever
    grants ``admin:read``+MFA. Returns CSRF headers for the resulting (rotated) session."""
    email = f"admin-{uuid.uuid4().hex[:10]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    with Session(db_engine) as session:
        create_admin(session, email, password)
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    csrf = _csrf_header(login)
    enroll = client.post("/account/mfa/enroll", headers=csrf)
    secret = enroll.json()["secret"]
    client.post("/account/mfa/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=csrf)
    step_up = client.post(
        "/account/mfa/step-up", json={"code": pyotp.TOTP(secret).now()}, headers=csrf
    )
    assert step_up.status_code == 200
    return _csrf_header(step_up)


# --- (1) mass assignment: role / is_admin / mfa_verified_at / user_id on a writable body ----


@pytest.mark.parametrize(
    "extra_field", ["role", "is_admin", "mfa_verified_at", "user_id", "admin", "permissions"]
)
def test_mass_assignment_on_register_is_rejected(api_env: Engine, extra_field: str) -> None:
    client = _client()
    payload = {**_credentials(), extra_field: "admin"}
    response = client.post("/auth/register", json=payload)
    # extra="forbid" (T12.3.6): the request is rejected outright, not silently created with
    # the extra field ignored -- see test_an_ordinary_user_can_never_reach_role_admin_in_the_
    # database below for the "ignored" half of the done-when, exercised independently.
    assert response.status_code == 422


@pytest.mark.parametrize("extra_field", ["role", "is_admin", "user_id"])
def test_mass_assignment_on_mfa_confirm_is_rejected(api_env: Engine, extra_field: str) -> None:
    client = _client()
    login = _register_and_login(client)
    csrf = _csrf_header(login)
    client.post("/account/mfa/enroll", headers=csrf)
    response = client.post(
        "/account/mfa/confirm", json={"code": "123456", extra_field: "admin"}, headers=csrf
    )
    assert response.status_code == 422


def test_an_ordinary_user_can_never_reach_role_admin_in_the_database(
    api_env: Engine, db_engine: Engine
) -> None:
    """End to end: whatever a registering client sends, the stored row's role is always
    ``user`` -- the admin bootstrap CLI (``rag_app.admin``) is the only writer."""
    client = _client()
    payload = {**_credentials(), "role": "admin"}
    client.post("/auth/register", json=payload)  # 422 (above) -- no row should even exist
    from sqlalchemy import select

    from rag_app.db.models import User

    with Session(db_engine) as session:
        row = session.scalar(select(User).where(User.email == payload["email"]))
        assert row is None or row.role == "user"


# --- (2) query/header tricks -----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Role": "admin"},
        {"X-User-Id": "00000000-0000-0000-0000-000000000000"},
        {"X-Admin": "true"},
        {"X-SecRAG-Client-IP": "127.0.0.1|9999999999|forged"},
    ],
)
def test_header_tricks_do_not_grant_admin_ping(api_env: Engine, headers: dict[str, str]) -> None:
    client = _client()
    _register_and_login(client)
    response = client.get("/admin/ping", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("query", ["?role=admin", "?admin=true", "?is_admin=1"])
def test_query_string_tricks_do_not_grant_admin_ping(api_env: Engine, query: str) -> None:
    client = _client()
    _register_and_login(client)
    response = client.get(f"/admin/ping{query}")
    assert response.status_code == 403


# --- (3) cookie tampering ---------------------------------------------------------------------


def test_an_edited_session_cookie_is_rejected(api_env: Engine) -> None:
    client = _client()
    login = _register_and_login(client)
    raw = login.cookies.get(SESSION_COOKIE_NAME)
    assert raw
    tampered = raw[:-1] + ("a" if raw[-1] != "a" else "b")
    response = client.get("/auth/me", cookies={SESSION_COOKIE_NAME: tampered})
    assert response.status_code == 401


def test_a_cookie_in_another_format_is_rejected(api_env: Engine) -> None:
    client = _client()
    response = client.get(
        "/auth/me", cookies={SESSION_COOKIE_NAME: '{"user_id": "x", "role": "admin"}'}
    )
    assert response.status_code == 401


def test_the_session_cookie_value_never_literally_encodes_the_role(
    api_env: Engine, db_engine: Engine
) -> None:
    """The cookie is an opaque random token (``secrets.token_urlsafe``); the role is read
    from the database on every request, never decoded from the cookie itself."""
    client = _client()
    email = f"admin-{uuid.uuid4().hex[:10]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    with Session(db_engine) as session:
        create_admin(session, email, password)
    login = client.post("/auth/login", json={"email": email, "password": password})
    raw = login.cookies.get(SESSION_COOKIE_NAME)
    assert raw and "admin" not in raw.lower() and "role" not in raw.lower()


# --- (4) direct calls, bypassing any "UI" -----------------------------------------------------


def test_a_user_session_calling_admin_ping_directly_gets_403(api_env: Engine) -> None:
    """Simulates a curl call straight at the backend with a stolen/valid USER session --
    no UI, no middleware.ts involved on this side of the wire at all."""
    client = _client()
    login = _register_and_login(client)
    response = client.get(
        "/admin/ping",
        cookies={SESSION_COOKIE_NAME: login.cookies.get(SESSION_COOKIE_NAME)},
    )
    assert response.status_code == 403


def test_no_session_calling_admin_ping_directly_gets_401(api_env: Engine) -> None:
    assert _client().get("/admin/ping").status_code == 401


# --- (6) admin without MFA step-up -------------------------------------------------------------


def test_an_admin_session_without_stepup_is_403_on_every_mfa_gated_route(
    api_env: Engine, db_engine: Engine
) -> None:
    client = _client()
    email = f"admin-{uuid.uuid4().hex[:10]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    with Session(db_engine) as session:
        create_admin(session, email, password)
    client.post("/auth/login", json={"email": email, "password": password})
    for route in PROTECTED_ROUTES:
        if not route.requires_mfa:
            continue
        response = client.request(route.method, route.path)
        assert response.status_code == 403, route


# --- the role x route matrix, generated from the permission map (T12.3.6) ---------------------


def test_the_permission_map_matrix_covers_every_protected_route(
    api_env: Engine, db_engine: Engine
) -> None:
    """For every route ``rag_app.authz`` actually gates, and every role: granted (and, if
    the route also needs MFA, stepped up) -> allowed; everything else -> denied. Built from
    ``PROTECTED_ROUTES`` so a FUTURE admin route is covered automatically, with no new test
    code -- only a new entry appears in the matrix below."""
    assert PROTECTED_ROUTES, "no protected route registered -- did /admin/ping lose its gate?"

    user_client = _client()
    _register_and_login(user_client)

    admin_plain_client = _client()  # admin, logged in, but never stepped up
    email = f"admin-{uuid.uuid4().hex[:10]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    with Session(db_engine) as session:
        create_admin(session, email, password)
    admin_plain_client.post("/auth/login", json={"email": email, "password": password})

    admin_stepped_client = _client()
    _stepped_up_admin(db_engine, admin_stepped_client)

    scenarios = [
        ("user", user_client, False),
        ("admin-no-stepup", admin_plain_client, False),
        ("admin-stepped-up", admin_stepped_client, True),
    ]

    exercised = 0
    for route in PROTECTED_ROUTES:
        for label, client, stepped_up in scenarios:
            role = "admin" if label.startswith("admin") else "user"
            granted = route.permission in ROLE_PERMISSIONS.get(role, frozenset())
            allowed = granted and (not route.requires_mfa or stepped_up)
            response = client.request(route.method, route.path)
            if allowed:
                assert response.status_code < 400, (label, route)
            else:
                assert response.status_code == 403, (label, route)
            exercised += 1
    # Sanity: the matrix actually ran something (never a silent no-op if the registry were
    # ever emptied by a refactor).
    assert exercised == len(PROTECTED_ROUTES) * len(scenarios)
