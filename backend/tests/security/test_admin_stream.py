"""Admin SSE stream (T13.4.1, local only): ``GET /admin/stream`` sends an immediate
"snapshot", then a "heartbeat" (fresh data, doubling as the liveness ping) every tick --
re-checking auth AND that MFA is set (not recent -- DA-13-C-5: ``mfa_verified_at`` is only
ever ``None``/not-``None``, there is no step-up TTL) on EVERY tick WITHOUT refreshing the
session's ``last_seen_at`` (a background tab must still idle out on schedule), idle-closing
after a bound regardless, and capping concurrent connections. DEBT-046 (the erasure gauges the
purger Job sets never reach the API's own ``/metrics``) is proven fixed here too: a tick
recomputes them from the database directly.

Two testing strategies, deliberately split:

* The two 403 cases (``matrix=False`` -- see ``rag_app.api.admin``'s module docstring for
  why) go through the real HTTP route (``TestClient``), like every other explicit 403 test
  in ``test_admin_api.py``.
* Everything about the PER-TICK re-check (role lost, MFA lost, session expired, idle close,
  DEBT-046) drives ``rag_app.api.admin._admin_stream_events`` directly as a plain Python
  async generator (``asyncio.run``, no pytest-asyncio dependency needed): calling
  ``__anext__()`` by hand gives full, race-free control over exactly when a mid-stream
  database mutation happens relative to each tick -- a real HTTP streaming client reading
  the SAME two events would be exercising httpx's/Starlette's transport plumbing, not this
  module's own logic, for a lot more test flakiness risk.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from rag_app import metrics as real_metrics
from rag_app.admin import create_admin
from rag_app.api import admin
from rag_app.api.app import create_app
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.config import get_settings
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, csrf_token
from rag_app.db.models import DeletionRequest, User
from rag_app.email_abuse import EmailAbuseLimiter
from rag_app.metrics_backend import InProcessMetricsBackend
from rag_app.risk import SignupTracker
from rag_app.sessions import SESSION_COOKIE_NAME, create_session, set_mfa_verified

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


@pytest.fixture(autouse=True)
def _reset_connection_count() -> None:
    # A module-level counter (rag_app.api.admin._admin_stream_connections) -- reset before
    # EVERY test in this file, not just the cap test, so an earlier test driving the
    # generator by hand (never going through a real disconnect) can never leak a stale
    # count into an unrelated later test.
    admin._admin_stream_connections = 0


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    app.dependency_overrides[get_email_abuse_limiter] = EmailAbuseLimiter
    return TestClient(app, base_url=BASE_URL)


def _creds() -> dict[str, str]:
    return {
        "email": f"admin-stream-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("PROMETHEUS_URL", "")
    return db_engine


def _admin_session(engine: Engine, *, stepped_up: bool) -> tuple[uuid.UUID, str]:
    """A fresh admin account with a live session; returns (user_id, raw_session_token)."""
    creds = _creds()
    with Session(engine) as session:
        user = create_admin(session, creds["email"], creds["password"])
        raw = create_session(session, user)
        if stepped_up:
            set_mfa_verified(session, raw)
        session.commit()
        return user.id, raw


def _admin_headers(raw: str) -> dict[str, str]:
    csrf = csrf_token(get_settings().csrf_secret, raw)
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={raw}; {CSRF_COOKIE_NAME}={csrf}",
        CSRF_HEADER_NAME: csrf,
    }


def _user_headers(client: TestClient) -> dict[str, str]:
    creds = _creds()
    register = client.post("/auth/register", json=creds)
    assert register.status_code == 202, register.text
    login = client.post("/auth/login", json=creds)
    assert login.status_code == 200, login.text
    token = login.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


# --- 403 cases (matrix=False -- explicit tests, same Gate line as T13.1.5/T13.1.8) ---------


def test_an_ordinary_user_cannot_open_the_admin_stream(api_env: Engine) -> None:
    client = _client()
    headers = _user_headers(client)
    response = client.get("/admin/stream", headers=headers)
    assert response.status_code == 403


def test_an_admin_without_mfa_step_up_cannot_open_the_admin_stream(api_env: Engine) -> None:
    client = _client()
    _, raw = _admin_session(api_env, stepped_up=False)
    response = client.get("/admin/stream", headers=_admin_headers(raw))
    assert response.status_code == 403


# --- The route itself: wiring, cap -----------------------------------------------------


def _fake_http(raw: str) -> object:
    class _FakeRequest:
        cookies = {SESSION_COOKIE_NAME: raw}

    return _FakeRequest()


async def _open_and_prime(raw: str, settings: admin.AdminStreamSettings) -> StreamingResponse:
    """Calls the route function directly (bypassing FastAPI's own dependency injection --
    fine here, since the 403 tests above already prove the permission+MFA gate; this is
    only exercising the connection-cap bookkeeping) and advances its generator once so the
    `finally` block that decrements the counter is reachable later (a generator that is
    never started does not run `finally` on `aclose()`)."""
    backend = InProcessMetricsBackend()
    response = await admin.admin_stream(_fake_http(raw), settings, backend)  # type: ignore[arg-type]
    await response.body_iterator.__anext__()  # consumes the immediate "snapshot" frame
    return response


def test_admin_stream_rejects_a_connection_once_the_cap_is_reached(api_env: Engine) -> None:
    _, raw = _admin_session(api_env, stepped_up=True)
    settings = admin.AdminStreamSettings(tick_seconds=10, idle_close_seconds=10, max_connections=1)

    async def _run() -> None:
        first = await _open_and_prime(raw, settings)
        try:
            with pytest.raises(HTTPException) as exc:
                await admin.admin_stream(_fake_http(raw), settings, InProcessMetricsBackend())
            assert exc.value.status_code == 503
        finally:
            await first.body_iterator.aclose()

    asyncio.run(_run())
    assert admin._admin_stream_connections == 0  # the finally above decremented it back


# --- The per-tick re-check, driven directly (see module docstring) -------------------------


def _drive(
    gen: object, steps: int
) -> list[str]:  # events, driven synchronously inside one asyncio.run
    async def _collect() -> list[str]:
        out = []
        for _ in range(steps):
            out.append(await gen.__anext__())  # type: ignore[union-attr]
        return out

    return asyncio.run(_collect())


def test_the_first_event_is_a_snapshot_and_the_second_is_a_heartbeat(api_env: Engine) -> None:
    _, raw = _admin_session(api_env, stepped_up=True)
    settings = admin.AdminStreamSettings(
        tick_seconds=0.01, idle_close_seconds=10, max_connections=10
    )
    gen = admin._admin_stream_events(raw, settings, InProcessMetricsBackend())
    first, second = _drive(gen, 2)
    assert first.startswith("event: snapshot\n")
    assert second.startswith("event: heartbeat\n")
    assert '"features"' in first and '"erasure"' in first


def test_the_stream_closes_when_the_role_is_lost_mid_stream(api_env: Engine) -> None:
    user_id, raw = _admin_session(api_env, stepped_up=True)
    settings = admin.AdminStreamSettings(
        tick_seconds=0.01, idle_close_seconds=10, max_connections=10
    )
    gen = admin._admin_stream_events(raw, settings, InProcessMetricsBackend())

    async def _run() -> tuple[str, str]:
        first = await gen.__anext__()
        with Session(api_env) as session:
            session.execute(update(User).where(User.id == user_id).values(role="user"))
            session.commit()
        second = await gen.__anext__()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        return first, second

    first, second = asyncio.run(_run())
    assert first.startswith("event: snapshot\n")
    assert second.startswith("event: closed\n")
    assert '"reason": "role_lost"' in second


def test_the_stream_closes_when_mfa_step_up_is_lost_mid_stream(api_env: Engine) -> None:
    _, raw = _admin_session(api_env, stepped_up=True)
    settings = admin.AdminStreamSettings(
        tick_seconds=0.01, idle_close_seconds=10, max_connections=10
    )
    gen = admin._admin_stream_events(raw, settings, InProcessMetricsBackend())

    async def _run() -> tuple[str, str]:
        first = await gen.__anext__()
        with Session(api_env) as session:
            from rag_app.db.models import UserSession
            from rag_app.security import hash_token

            session.execute(
                update(UserSession)
                .where(UserSession.id == hash_token(raw))
                .values(mfa_verified_at=None)
            )
            session.commit()
        second = await gen.__anext__()
        return first, second

    first, second = asyncio.run(_run())
    assert first.startswith("event: snapshot\n")
    assert second.startswith("event: closed\n")
    assert '"reason": "mfa_step_up_lost"' in second


def test_the_stream_closes_when_the_session_itself_expires_mid_stream(api_env: Engine) -> None:
    """The ``check_still_valid`` re-check, not the idle-close bound below -- a session whose
    OWN absolute/idle expiry has simply passed closes the stream for that reason, distinct
    from "this one connection has been open too long"."""
    _, raw = _admin_session(api_env, stepped_up=True)
    settings = admin.AdminStreamSettings(
        tick_seconds=0.01, idle_close_seconds=10, max_connections=10
    )
    gen = admin._admin_stream_events(raw, settings, InProcessMetricsBackend())

    async def _run() -> tuple[str, str]:
        first = await gen.__anext__()
        with Session(api_env) as session:
            from rag_app.db.models import UserSession
            from rag_app.security import hash_token

            session.execute(
                update(UserSession)
                .where(UserSession.id == hash_token(raw))
                .values(idle_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
            )
            session.commit()
        second = await gen.__anext__()
        return first, second

    first, second = asyncio.run(_run())
    assert first.startswith("event: snapshot\n")
    assert second.startswith("event: closed\n")
    assert '"reason": "session_expired"' in second


def test_the_stream_closes_on_idle_timeout_even_though_everything_else_stays_valid(
    api_env: Engine,
) -> None:
    _, raw = _admin_session(api_env, stepped_up=True)
    # idle_close_seconds smaller than tick_seconds: the SECOND loop iteration's own idle-
    # close check (at the top of the loop, before the next DB round trip) must already fire.
    settings = admin.AdminStreamSettings(
        tick_seconds=0.05, idle_close_seconds=0.03, max_connections=10
    )
    gen = admin._admin_stream_events(raw, settings, InProcessMetricsBackend())
    first, second = _drive(gen, 2)
    assert first.startswith("event: snapshot\n")
    assert second.startswith("event: closed\n")
    assert '"reason": "idle_timeout"' in second


def test_a_stream_tick_refreshes_the_erasure_gauges_from_the_database(api_env: Engine) -> None:
    """DEBT-046: a value the purger Job would have set in ITS OWN process (never scraped by
    the API's ``/metrics``) must instead be recomputed by the API itself on every tick --
    proven here by inserting an open tombstone with RAW SQL (bypassing ``request_erasure``,
    which is the only OTHER thing that calls ``refresh_erasure_gauges`` in-process) and
    forcing the gauge to a stale 0 first."""
    user_id, raw = _admin_session(api_env, stepped_up=True)
    with Session(api_env) as session:
        session.add(
            DeletionRequest(
                id=uuid.uuid4(),
                user_id=user_id,
                requested_at=dt.datetime.now(dt.UTC),
                status="pending",
                progress={},
                attempts=0,
                updated_at=dt.datetime.now(dt.UTC),
            )
        )
        session.commit()
    real_metrics.ERASURE_PENDING.set(0)  # simulate "never refreshed in this process"

    settings = admin.AdminStreamSettings(tick_seconds=10, idle_close_seconds=10, max_connections=10)
    gen = admin._admin_stream_events(raw, settings, InProcessMetricsBackend())
    _drive(gen, 1)

    assert real_metrics.REGISTRY.get_sample_value("secrag_erasure_pending") >= 1
