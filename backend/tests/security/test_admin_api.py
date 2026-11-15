"""Admin dashboard API (T13.1.1-T13.1.8): features/metrics/eval/erasure read endpoints,
purge-now/retry, and the admin erase-account action (DEBT-002). The role x route tampering
matrix (``test_client_tampering.py``) already proves 403/MFA for every ``matrix=True`` route
registered here (``GET /admin/features`` etc, T13.1.1's "no hand-written 403 tests") -- the
tests below cover: the actual response shape, the no-PII rule (T13.1.7), and the three
mutating routes that need a path parameter or a body (``matrix=False``), whose own Gate line
(T13.1.5/T13.1.8) asks for explicit 403 coverage.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from rag_app.admin import create_admin
from rag_app.api.app import create_app
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.config import get_settings
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, csrf_token
from rag_app.db.models import AdminAction, DeletionRequest, User
from rag_app.email_abuse import EmailAbuseLimiter
from rag_app.risk import SignupTracker
from rag_app.sessions import SESSION_COOKIE_NAME, create_session, set_mfa_verified

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    app.dependency_overrides[get_email_abuse_limiter] = EmailAbuseLimiter
    return TestClient(app, base_url=BASE_URL)


def _creds() -> dict[str, str]:
    return {
        "email": f"admin-api-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def _admin_headers(db_engine: Engine, *, stepped_up: bool) -> dict[str, str]:
    """A fresh admin account, with a live session -- MFA-stepped-up or not, set DIRECTLY on
    the session row (no TOTP round trip, faster/more deterministic than
    ``test_client_tampering.py``'s ``_stepped_up_admin``; both approaches exercise the same
    ``require_permission_and_mfa`` gate, which only ever reads ``mfa_verified_at``)."""
    creds = _creds()
    with Session(db_engine) as session:
        user = create_admin(session, creds["email"], creds["password"])
        raw = create_session(session, user)
        if stepped_up:
            set_mfa_verified(session, raw)
        session.commit()
    csrf = csrf_token(get_settings().csrf_secret, raw)
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={raw}; {CSRF_COOKIE_NAME}={csrf}",
        CSRF_HEADER_NAME: csrf,
    }


def _user_headers(client: TestClient) -> dict[str, str]:
    """An ordinary, logged-in user (never admin) -- register + login, same as
    ``test_client_tampering.py``'s ``_register_and_login``, returning request headers
    (session + CSRF) instead of relying on the client's cookie jar."""
    creds = _creds()
    register = client.post("/auth/register", json=creds)
    assert register.status_code == 202, register.text
    login = client.post("/auth/login", json=creds)
    assert login.status_code == 200, login.text
    token = login.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("PROMETHEUS_URL", "")
    return db_engine


def _make_live_user(engine: Engine, email: str) -> uuid.UUID:
    """A LIVE (never erased) user with conversations/messages, the same shape
    ``test_erasure.py``'s fixtures use, so the erasure endpoints have something real to show."""
    from rag_app.crypto import generate_user_key, wrap_key
    from rag_app.db.models import Conversation, Message, UserKey
    from rag_app.security import hash_password

    with Session(engine) as session:
        user = User(email=email, password_hash=hash_password("irrelevant"), email_verified=True)
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
        conv = Conversation(user_id=user.id)
        session.add(conv)
        session.flush()
        session.add(Message(conversation_id=conv.id, role="user", content_encrypted=b"x"))
        session.commit()
        return user.id


# --- GET /admin/features, /admin/metrics, /admin/eval, /admin/erasure (matrix=True) -------


def test_admin_features_lists_every_registry_entry(api_env: Engine) -> None:
    from rag_app.features import REGISTRY

    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/features", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["features"]) == len(REGISTRY)
    ids = {f["id"] for f in body["features"]}
    assert "admin-api" in ids and "privacy-asynchronous-erasure" in ids


def test_admin_metrics_returns_a_value_per_declared_metric_name(api_env: Engine) -> None:
    from rag_app.features import REGISTRY

    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/metrics", headers=headers)
    assert response.status_code == 200, response.text
    declared = {name for entry in REGISTRY for name in entry.metrics}
    assert declared <= set(response.json()["values"])


def test_admin_eval_reports_aggregate_metrics_only(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/eval", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["key"] for row in body["metrics"]} == {
        "retrieval_recall",
        "faithfulness",
        "correctness",
        "correct_abstention",
    }
    # T13.1.7: never a per-question golden-set text, only the metric rows above.
    assert "question" not in response.text and "answer" not in response.text


def test_admin_erasure_lists_an_open_request_with_rows_remaining(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    user_id = _make_live_user(api_env, f"erase-target-{uuid.uuid4().hex[:8]}@example.test")
    with Session(api_env) as session:
        from rag_app.erasure import request_erasure

        assert request_erasure(session, user_id) is True

    response = client.get("/admin/erasure", headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()["requests"]
    mine = [r for r in rows if r["status"] == "pending" and r["rows_remaining"]["messages"] == 1]
    assert mine, rows
    assert mine[0]["id"]  # opaque id present
    assert mine[0]["attempts"] == 0
    assert mine[0]["overdue"] is False


# --- POST /admin/erasure/{id}/purge-now and /retry (matrix=False, explicit 403s) -----------


def _open_request_id(engine: Engine, email: str) -> str:
    from rag_app.erasure import request_erasure

    user_id = _make_live_user(engine, email)
    with Session(engine) as session:
        assert request_erasure(session, user_id) is True
        request_id = session.scalar(
            select(DeletionRequest.id).where(DeletionRequest.user_id == user_id)
        )
    assert request_id is not None
    return str(request_id)


def test_purge_now_runs_the_purger_in_the_background_and_logs_an_admin_action(
    api_env: Engine,
) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    request_id = _open_request_id(api_env, f"purge-now-{uuid.uuid4().hex[:8]}@example.test")

    response = client.post(f"/admin/erasure/{request_id}/purge-now", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["request_id"] == request_id

    with Session(api_env) as session:
        action = session.scalar(
            select(AdminAction).where(AdminAction.action == "erasure_purge_now")
        )
        assert action is not None
        assert action.target == request_id
        assert "@" not in (action.target or "")  # opaque id only, never an email


def test_purge_now_on_an_unknown_request_is_404(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post(f"/admin/erasure/{uuid.uuid4()}/purge-now", headers=headers)
    assert response.status_code == 404


def test_retry_refuses_a_request_that_is_not_failed(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    request_id = _open_request_id(api_env, f"retry-{uuid.uuid4().hex[:8]}@example.test")
    response = client.post(f"/admin/erasure/{request_id}/retry", headers=headers)
    assert response.status_code == 409


def test_a_plain_user_gets_403_on_every_erasure_action(api_env: Engine) -> None:
    client = _client()
    headers = _user_headers(client)
    request_id = _open_request_id(api_env, f"user-403-{uuid.uuid4().hex[:8]}@example.test")
    for path in (
        f"/admin/erasure/{request_id}/purge-now",
        f"/admin/erasure/{request_id}/retry",
    ):
        response = client.post(path, headers=headers)
        assert response.status_code == 403, (path, response.text)


def test_an_admin_without_mfa_stepup_gets_403_on_every_erasure_action(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=False)
    request_id = _open_request_id(api_env, f"no-mfa-{uuid.uuid4().hex[:8]}@example.test")
    for path in (
        f"/admin/erasure/{request_id}/purge-now",
        f"/admin/erasure/{request_id}/retry",
    ):
        response = client.post(path, headers=headers)
        assert response.status_code == 403, (path, response.text)


# --- POST /admin/users/erase (T13.1.8, DEBT-002) -------------------------------------------


def test_admin_can_erase_another_account_and_the_request_reaches_done(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    target_email = f"orphan-{uuid.uuid4().hex[:8]}@example.test"
    target_id = _make_live_user(api_env, target_email)

    response = client.post(
        "/admin/users/erase",
        json={"email": target_email, "confirmation": "ERASE"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    request_id = body["request_id"]
    assert target_email not in response.text  # T13.1.7: never echoed back

    with Session(api_env) as session:
        action = session.scalar(select(AdminAction).where(AdminAction.action == "erase_user"))
        assert action is not None and action.target == request_id
        assert target_email not in (action.target or "")

        from rag_app.purger import Purger

        result = Purger(api_env).run(only=uuid.UUID(request_id))
        assert result.done == 1, result.summary()
        row = session.get(DeletionRequest, uuid.UUID(request_id))
        assert row is not None and row.status == "done"
        # A fully completed purge removes the "users" row itself (the final, leaf-last
        # step) -- not merely scrubbed, GONE.
        assert session.get(User, target_id) is None


def test_erase_user_requires_the_exact_confirmation_phrase(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    target_email = f"bad-confirm-{uuid.uuid4().hex[:8]}@example.test"
    _make_live_user(api_env, target_email)
    response = client.post(
        "/admin/users/erase",
        json={"email": target_email, "confirmation": "erase"},  # wrong case
        headers=headers,
    )
    assert response.status_code == 400


def test_erase_user_on_an_unknown_email_is_404_not_500(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post(
        "/admin/users/erase",
        json={"email": "nobody-here@example.test", "confirmation": "ERASE"},
        headers=headers,
    )
    assert response.status_code == 404


def test_a_plain_user_gets_403_on_erase_user(api_env: Engine) -> None:
    client = _client()
    headers = _user_headers(client)
    target_email = f"user-cant-{uuid.uuid4().hex[:8]}@example.test"
    _make_live_user(api_env, target_email)
    response = client.post(
        "/admin/users/erase",
        json={"email": target_email, "confirmation": "ERASE"},
        headers=headers,
    )
    assert response.status_code == 403


def test_an_admin_without_mfa_stepup_gets_403_on_erase_user(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=False)
    target_email = f"no-mfa-erase-{uuid.uuid4().hex[:8]}@example.test"
    _make_live_user(api_env, target_email)
    response = client.post(
        "/admin/users/erase",
        json={"email": target_email, "confirmation": "ERASE"},
        headers=headers,
    )
    assert response.status_code == 403


# --- T13.1.7: no-PII responses, scanned across every admin endpoint ------------------------


def test_no_admin_response_ever_echoes_a_submitted_email(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    target_email = f"no-pii-{uuid.uuid4().hex[:8]}@example.test"
    _make_live_user(api_env, target_email)
    responses = [
        client.get("/admin/features", headers=headers),
        client.get("/admin/metrics", headers=headers),
        client.get("/admin/eval", headers=headers),
        client.get("/admin/erasure", headers=headers),
        client.post(
            "/admin/users/erase",
            json={"email": target_email, "confirmation": "ERASE"},
            headers=headers,
        ),
    ]
    for response in responses:
        assert target_email not in response.text, response.text
