"""DEC-050 (T13.J4-fix, DA-13-J3J4-5): the Ollama warm-up (``rag_app.warmup.trigger_warm_up``)
is triggered by the API lifespan at start-up (unchanged, see test_startup.py), a SUCCESSFUL
``POST /auth/login``, and an authenticated ``GET /conversations`` (the call the frontend's
``/chat`` page already makes on every page load with a valid session) -- NEVER from an
unauthenticated request. ``GET /wake`` itself no longer calls it at all (test_wake.py's own
negative control).
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from rag_app.api.app import create_app
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.email_abuse import EmailAbuseLimiter
from rag_app.risk import SignupTracker

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


def _creds() -> dict[str, str]:
    return {
        "email": f"warmup-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def test_a_successful_login_triggers_the_ollama_warm_up(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("rag_app.api.auth.warmup.trigger_warm_up", lambda *a: calls.append(a) or True)
    client = _client()
    creds = _creds()
    assert client.post("/auth/register", json=creds).status_code == 202
    response = client.post("/auth/login", json=creds)
    assert response.status_code == 200, response.text
    assert calls == [()]


def test_a_failed_login_never_triggers_the_ollama_warm_up(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("rag_app.api.auth.warmup.trigger_warm_up", lambda *a: calls.append(a) or True)
    client = _client()
    creds = _creds()
    assert client.post("/auth/register", json=creds).status_code == 202
    response = client.post("/auth/login", json={**creds, "password": "wrong password entirely"})
    assert response.status_code == 401
    assert calls == [], "a failed login must never trigger the Ollama warm-up"


def test_login_for_an_unknown_email_never_triggers_the_ollama_warm_up(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("rag_app.api.auth.warmup.trigger_warm_up", lambda *a: calls.append(a) or True)
    client = _client()
    response = client.post("/auth/login", json=_creds())
    assert response.status_code == 401
    assert calls == []


def _make_user(engine: Engine) -> uuid.UUID:
    """A user authenticated WITHOUT going through ``/auth/login`` -- login's own success
    path also calls ``trigger_warm_up`` (above), which would double-count a call the
    conversations-route tests below mean to attribute to `GET /conversations` alone (both
    patch the SAME underlying `rag_app.warmup.trigger_warm_up` function, module-path string
    used to locate it notwithstanding)."""
    from rag_app.crypto import generate_user_key, wrap_key
    from rag_app.db.models import User, UserKey
    from rag_app.security import hash_password

    with Session(engine) as session:
        user = User(
            email=f"warmup-conv-{uuid.uuid4().hex[:10]}@example.test",
            password_hash=hash_password("correct horse battery"),
        )
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
        session.commit()
        return user.id


def test_an_authenticated_conversations_list_triggers_the_ollama_warm_up(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frontend's `/chat` page calls `GET /conversations` on every page load with a
    valid session (`refreshConversations`, `frontend/app/chat/page.tsx`) -- this is the
    "authenticated /chat page load" trigger point DEC-050 names."""
    calls: list[object] = []
    monkeypatch.setattr(
        "rag_app.api.conversations.warmup.trigger_warm_up", lambda *a: calls.append(a) or True
    )
    from conftest import cookie_auth_headers

    client = _client()
    user_id = _make_user(api_env)
    headers = cookie_auth_headers(api_env, user_id)
    response = client.get("/conversations", headers={"Cookie": headers["Cookie"]})
    assert response.status_code == 200, response.text
    assert calls == [()]


def test_an_unauthenticated_conversations_list_never_triggers_the_ollama_warm_up(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control (DEC-050's own Done-when): no session at all must never reach the
    warm-up trigger -- the route's own auth dependency rejects the request first."""
    calls: list[object] = []
    monkeypatch.setattr(
        "rag_app.api.conversations.warmup.trigger_warm_up", lambda *a: calls.append(a) or True
    )
    response = _client().get("/conversations")
    assert response.status_code == 401
    assert calls == []
