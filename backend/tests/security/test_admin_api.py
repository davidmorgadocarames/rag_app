"""Admin dashboard API (T13.1.1-T13.1.8): features/metrics/eval/erasure read endpoints,
purge-now/retry, and the admin erase-account action (DEBT-002). The role x route tampering
matrix (``test_client_tampering.py``) already proves 403/MFA for every ``matrix=True`` route
registered here (``GET /admin/features`` etc, T13.1.1's "no hand-written 403 tests") -- the
tests below cover: the actual response shape, the no-PII rule (T13.1.7), the three mutating
routes that need a path parameter or a body (``matrix=False``), whose own Gate line
(T13.1.5/T13.1.8) asks for explicit 403 coverage, their CSRF protection specifically
(DA-13-B-1, fix block 13-B-F -- ``test_csrf.py`` is scoped to ``/auth/logout``/``/account``
only, never these three), and the last-remaining-admin guard on ``/admin/users/erase``
(DEC-033, same block).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session

from rag_app.admin import create_admin
from rag_app.api.app import create_app
from rag_app.api.deps import get_email_abuse_limiter, get_signup_tracker
from rag_app.config import get_settings
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, csrf_token
from rag_app.db.models import AdminAction, DeletionRequest, User
from rag_app.email_abuse import EmailAbuseLimiter
from rag_app.erasure import ERASURE_RETRY_AFTER_SECONDS
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


def _admin_session_with_email(
    engine: Engine, *, stepped_up: bool = True
) -> tuple[str, dict[str, str]]:
    """Like ``_admin_headers`` but also returns the created admin's own email -- needed for
    the self-erase tests below, where the caller must submit its own address as the target."""
    creds = _creds()
    with Session(engine) as session:
        user = create_admin(session, creds["email"], creds["password"])
        raw = create_session(session, user)
        if stepped_up:
            set_mfa_verified(session, raw)
        session.commit()
    csrf = csrf_token(get_settings().csrf_secret, raw)
    headers = {
        "Cookie": f"{SESSION_COOKIE_NAME}={raw}; {CSRF_COOKIE_NAME}={csrf}",
        CSRF_HEADER_NAME: csrf,
    }
    return creds["email"], headers


def _retire_every_live_admin(engine: Engine) -> None:
    """Test-only clean slate for the last-admin-guard tests: ``db_url`` is session-scoped (one
    physical database for the whole pytest run), so by the time these tests run, earlier tests
    in this file have already created several admin accounts that are still "live" (never
    erased). Marks them as already gone -- ``deleted_at`` only, NOT a real erasure (no email
    scrub, no session revoke, no tombstone row) -- so "only remaining admin" is deterministic
    regardless of execution order or how many other admin accounts exist."""
    with Session(engine) as session:
        session.execute(
            update(User)
            .where(User.role == "admin", User.deleted_at.is_(None))
            .values(deleted_at=dt.datetime.now(dt.UTC))
        )
        session.commit()


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


def test_admin_latency_reports_per_stage_percentiles(api_env: Engine) -> None:
    from rag_app import metrics as real_metrics

    real_metrics.observe_stage("embed", 0.2)
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/latency", headers=headers)
    assert response.status_code == 200, response.text
    stages = {row["stage"]: row for row in response.json()["stages"]}
    assert "embed" in stages
    assert stages["embed"]["count"] >= 1
    assert stages["embed"]["p50_seconds"] is not None
    assert stages["embed"]["p95_seconds"] is not None


def test_admin_latency_pipeline_lists_every_allowlisted_step_in_order(api_env: Engine) -> None:
    """T13.8.2: ``pipeline`` has one entry per ``STEP_ALLOWLIST`` stage, in order, each with
    every one of its allow-listed steps (observed or not -- the waterfall's rows never
    shuffle); a step actually observed in this process shows a real count/avg/p50/p95."""
    from rag_app import metrics as real_metrics

    real_metrics.observe_step("retrieving", "embed_query", 0.03)
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/latency", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [stage["stage"] for stage in body["pipeline"]] == [
        name for name, _steps in real_metrics.STEP_ALLOWLIST
    ]
    retrieving = next(s for s in body["pipeline"] if s["stage"] == "retrieving")
    steps_by_name = {s["step"]: s for s in retrieving["steps"]}
    assert list(steps_by_name) == list(
        next(steps for name, steps in real_metrics.STEP_ALLOWLIST if name == "retrieving")
    )
    assert steps_by_name["embed_query"]["count"] >= 1
    assert steps_by_name["embed_query"]["avg_seconds"] is not None
    # An allow-listed step never observed yet still appears, with a zero/None placeholder.
    never_observed = steps_by_name["load_chunks"]
    if never_observed["count"] == 0:
        assert never_observed["avg_seconds"] is None


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
    assert mine[0]["completed_at"] is None  # T13.8.3(b): never set while still open


def test_admin_erasure_exposes_completed_at_for_done_rows(api_env: Engine) -> None:
    """T13.8.3(b): once a request is ``done``, its completion timestamp is exposed too --
    the column already existed (written by the purger), no migration needed."""
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    user_id = _make_live_user(api_env, f"erase-done-{uuid.uuid4().hex[:8]}@example.test")
    with Session(api_env) as session:
        from rag_app.erasure import request_erasure

        assert request_erasure(session, user_id) is True
        request_id = session.execute(
            text("SELECT id FROM deletion_requests WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()

    with api_env.begin() as conn:
        conn.execute(
            text(
                "UPDATE deletion_requests SET status = 'done', completed_at = now()"
                " WHERE id = :i"
            ),
            {"i": request_id},
        )

    response = client.get("/admin/erasure", headers=headers)
    assert response.status_code == 200, response.text
    rows = {r["id"]: r for r in response.json()["requests"]}
    row = rows[str(request_id)]
    assert row["status"] == "done"
    assert row["completed_at"] is not None


def test_admin_features_carries_the_alerts_from_a_single_probe_sweep(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-13-E-2: ``/admin/features`` returns the banner alerts too, built from the SAME probe
    sweep -- so the dashboard's polling tick runs the registry probes once, not twice (it used
    to call ``/admin/features`` and ``/admin/alerts`` in parallel, each sweeping on its own)."""
    from rag_app.api import admin as admin_module

    _close_every_open_erasure_request(api_env)
    request_id = _open_erasure_request_id(
        api_env, f"feat-alert-{uuid.uuid4().hex[:8]}@example.test"
    )
    _age_request(api_env, request_id, 25)

    sweeps = 0
    real_sweep = admin_module._features_payload

    def counting_sweep(metrics_backend):  # noqa: ANN001, ANN202
        nonlocal sweeps
        sweeps += 1
        return real_sweep(metrics_backend)

    monkeypatch.setattr(admin_module, "_features_payload", counting_sweep)
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/features", headers=headers)
    assert response.status_code == 200, response.text
    assert sweeps == 1
    alerts = {a["code"]: a for a in response.json()["alerts"]}
    assert alerts["erasure_overdue"]["severity"] == "red"
    _close_every_open_erasure_request(api_env)


# --- GET /admin/alerts (T13.5.2, R6-1, P13-D4): banner alerts, matrix=True ----------------


def _age_request(engine: Engine, request_id: uuid.UUID, hours: float) -> None:
    """Same pattern as ``test_erasure.py``'s own ``_age``: push a request's ``requested_at``
    back in time without waiting for real time to pass."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE deletion_requests SET requested_at = now() - make_interval(hours => :h)"
                " WHERE id = :i"
            ),
            {"h": hours, "i": request_id},
        )


def _open_erasure_request_id(engine: Engine, email: str) -> uuid.UUID:
    user_id = _make_live_user(engine, email)
    with Session(engine) as session:
        from rag_app.erasure import request_erasure

        assert request_erasure(session, user_id) is True
        found = session.execute(
            text("SELECT id FROM deletion_requests WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        return uuid.UUID(str(found))


def _close_every_open_erasure_request(engine: Engine) -> None:
    """``db_engine`` is shared across every ``db``-marked test in this session (T11.0.14) --
    an open (possibly aged-back) erasure request another test left behind would otherwise
    leak into THIS test's "oldest open request" aggregate. Defensive baseline, not a
    behaviour under test."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE deletion_requests SET status = 'done' WHERE status != 'done'"))


def test_admin_alerts_is_empty_with_a_clean_stack(api_env: Engine) -> None:
    """Done-when (T13.5.2): nothing wrong -> no banner at all. ``start_metrics_server`` is
    idempotent (``rag_app.metrics`` docstring) -- the real app's own lifespan calls it too;
    this ``TestClient`` never runs a lifespan, so the ``stack-prometheus`` probe would
    otherwise always read "down" here for a reason that has nothing to do with this test."""
    from rag_app import metrics as real_metrics

    real_metrics.start_metrics_server(0, addr="127.0.0.1")
    _close_every_open_erasure_request(api_env)
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/alerts", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["alerts"] == []


def test_admin_alerts_fires_red_past_the_24h_erasure_deadline(api_env: Engine) -> None:
    _close_every_open_erasure_request(api_env)
    request_id = _open_erasure_request_id(api_env, f"alert-red-{uuid.uuid4().hex[:8]}@example.test")
    _age_request(api_env, request_id, 25)

    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/alerts", headers=headers)
    alerts = {a["code"]: a for a in response.json()["alerts"]}
    assert alerts["erasure_overdue"]["severity"] == "red"
    # T13.8.3(a): the drawer for the erasure feature needs to find this alert.
    assert alerts["erasure_overdue"]["feature_ids"] == ["privacy-asynchronous-erasure"]


def test_admin_alerts_fires_amber_past_the_20h_erasure_warning(api_env: Engine) -> None:
    _close_every_open_erasure_request(api_env)
    request_id = _open_erasure_request_id(
        api_env, f"alert-amber-{uuid.uuid4().hex[:8]}@example.test"
    )
    _age_request(api_env, request_id, 21)

    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/alerts", headers=headers)
    alerts = {a["code"]: a for a in response.json()["alerts"]}
    assert alerts["erasure_approaching"]["severity"] == "amber"
    assert alerts["erasure_approaching"]["feature_ids"] == ["privacy-asynchronous-erasure"]
    assert "erasure_overdue" not in alerts


def test_admin_alerts_fires_red_when_the_daily_answer_cap_is_reached(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_app.usage_cap import reserve_answer

    monkeypatch.setenv("DAILY_ANSWER_CAP", "2")
    with Session(api_env) as session:
        assert reserve_answer(session, 2) is True
        assert reserve_answer(session, 2) is True  # at the cap now

    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/alerts", headers=headers)
    alerts = {a["code"]: a for a in response.json()["alerts"]}
    assert alerts["daily_cap_reached"]["severity"] == "red"
    assert "2/2" in alerts["daily_cap_reached"]["message"]
    # T13.8.9 (DA-13-G1-5): the closest EXISTING abuse/cost-control registry entry, not the
    # generation/groundedness feature (unrelated to cost) the original mapping used.
    assert alerts["daily_cap_reached"]["feature_ids"] == ["abuse-rate-limiting"]


def test_admin_alerts_stays_empty_below_the_daily_answer_cap(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_app import metrics as real_metrics
    from rag_app.usage_cap import reserve_answer

    real_metrics.start_metrics_server(0, addr="127.0.0.1")
    _close_every_open_erasure_request(api_env)
    monkeypatch.setenv("DAILY_ANSWER_CAP", "5")
    with Session(api_env) as session:
        assert reserve_answer(session, 5) is True

    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/alerts", headers=headers)
    assert response.json()["alerts"] == []


def test_banner_alerts_lockout_spike_threshold(api_env: Engine) -> None:
    """Unit-level, through a fake MetricsBackend (never the real shared counter -- avoids
    depending on how many OTHER tests in this session happened to hit the real lockout
    metric first)."""
    from rag_app.api.admin import LOCKOUT_SPIKE_THRESHOLD, _banner_alerts

    class _FakeMetricsBackend:
        def __init__(self, values: dict[str, float | None]) -> None:
            self._values = values

        def current_value(self, metric_name: str) -> float | None:
            return self._values.get(metric_name)

        def current_values(self, metric_names: Sequence[str]) -> dict[str, float | None]:
            return {name: self.current_value(name) for name in metric_names}

    with Session(api_env) as session:
        below = _banner_alerts(
            session,
            _FakeMetricsBackend({"secrag_lockout_hits_total": LOCKOUT_SPIKE_THRESHOLD - 1}),
            [],
        )
        at = _banner_alerts(
            session,
            _FakeMetricsBackend({"secrag_lockout_hits_total": LOCKOUT_SPIKE_THRESHOLD}),
            [],
        )
    assert not any(a.code == "lockout_spike" for a in below)
    assert any(a.code == "lockout_spike" and a.severity == "amber" for a in at)
    assert any(a.code == "lockout_spike" and a.feature_ids == ["identity-lockout"] for a in at)


def test_banner_alerts_email_failures_any_nonzero(api_env: Engine) -> None:
    from rag_app.api.admin import _banner_alerts

    class _FakeMetricsBackend:
        def __init__(self, value: float) -> None:
            self._value = value

        def current_value(self, metric_name: str) -> float | None:
            return self._value if metric_name == "secrag_email_failed_total" else None

        def current_values(self, metric_names: Sequence[str]) -> dict[str, float | None]:
            return {name: self.current_value(name) for name in metric_names}

    with Session(api_env) as session:
        clean = _banner_alerts(session, _FakeMetricsBackend(0.0), [])
        failing = _banner_alerts(session, _FakeMetricsBackend(3.0), [])
    assert not any(a.code == "email_failures" for a in clean)
    assert any(a.code == "email_failures" and a.severity == "amber" for a in failing)
    assert any(
        a.code == "email_failures" and a.feature_ids == ["identity-email-verification-reset"]
        for a in failing
    )


def test_banner_alerts_probe_failures_exclude_dev_only_and_azure_only(api_env: Engine) -> None:
    """DEBT-017/T13.3.1 precedent: a dev_only/azure_only feature reporting ``down`` locally
    (lab off, or an Azure-only check run off-Azure) is EXPECTED, never a real failure.

    T13.8.9 (DEBT-053, DA-13-G2-2): ALSO a negative control -- a dev_only/azure_only feature
    the admin explicitly turned ON here (``expected_here=True``) that is STILL ``down`` IS a
    real failure and must alert, never excused just because the feature is conditional in
    general. Before this fix, the exclusion was keyed on the static ``dev_only``/
    ``azure_only`` flags alone and could not tell the two cases apart."""
    from rag_app.api.admin import _banner_alerts
    from rag_app.api.schemas import FeatureHealthOut

    class _NoMetrics:
        def current_value(self, metric_name: str) -> float | None:
            return None

        def current_values(self, metric_names: Sequence[str]) -> dict[str, float | None]:
            return {}

    def _feature(
        id_: str,
        *,
        dev_only: bool = False,
        azure_only: bool = False,
        expected_here: bool = True,
    ) -> FeatureHealthOut:
        return FeatureHealthOut(
            id=id_,
            name=id_,
            phase="13",
            status="enabled",
            probe_status="down",
            probe_detail="test",
            metrics={},
            dev_only=dev_only,
            azure_only=azure_only,
            expected_here=expected_here,
            docs="docs/TRD.md",
        )

    with Session(api_env) as session:
        excused = _banner_alerts(
            session,
            _NoMetrics(),
            [
                _feature("lab", dev_only=True, expected_here=False),
                _feature("azure-thing", azure_only=True, expected_here=False),
            ],
        )
        real = _banner_alerts(session, _NoMetrics(), [_feature("stack-postgres")])
        multiple = _banner_alerts(
            session, _NoMetrics(), [_feature("stack-postgres"), _feature("stack-migrations")]
        )
        # Negative control (DEBT-053): expected_here=True + down must alert even though the
        # feature is dev_only -- the admin turned it on here and it is genuinely broken.
        turned_on_but_broken = _banner_alerts(
            session, _NoMetrics(), [_feature("lab", dev_only=True, expected_here=True)]
        )
    assert not any(a.code == "probe_failures" for a in excused)
    assert any(a.code == "probe_failures" and a.severity == "red" for a in real)
    # T13.8.3(a): probe_failures lists the ACTUAL failing feature ids, never a static guess.
    assert next(a for a in real if a.code == "probe_failures").feature_ids == ["stack-postgres"]
    assert set(next(a for a in multiple if a.code == "probe_failures").feature_ids) == {
        "stack-postgres",
        "stack-migrations",
    }
    assert any(
        a.code == "probe_failures" and a.feature_ids == ["lab"] for a in turned_on_but_broken
    )


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


# --- DA-13-B-1: all 3 mutating routes need a valid CSRF token, not merely a valid, stepped- -
# --- up admin session (fix block 13-B-F). Every existing test above always goes through ----
# --- ``_admin_headers``, which always attaches a VALID CSRF cookie+header -- there was no ---
# --- "admin, stepped-up, missing/wrong CSRF" case anywhere before this. ---------------------


def _mutating_admin_routes(request_id: str) -> tuple[tuple[str, dict[str, str] | None], ...]:
    return (
        (f"/admin/erasure/{request_id}/purge-now", None),
        (f"/admin/erasure/{request_id}/retry", None),
        ("/admin/users/erase", {"email": "whoever-csrf@example.test", "confirmation": "ERASE"}),
    )


def test_admin_mutating_routes_reject_a_missing_csrf_token(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    request_id = _open_request_id(api_env, f"csrf-missing-{uuid.uuid4().hex[:8]}@example.test")
    cookie_only = {"Cookie": headers["Cookie"]}  # no X-CSRF-Token header at all
    for path, body in _mutating_admin_routes(request_id):
        response = client.post(path, headers=cookie_only, json=body)
        assert response.status_code == 403, (path, response.text)


def test_admin_mutating_routes_reject_a_wrong_csrf_token(api_env: Engine) -> None:
    client = _client()
    headers = _admin_headers(api_env, stepped_up=True)
    request_id = _open_request_id(api_env, f"csrf-wrong-{uuid.uuid4().hex[:8]}@example.test")
    wrong = {"Cookie": headers["Cookie"], CSRF_HEADER_NAME: "not-the-real-token"}
    for path, body in _mutating_admin_routes(request_id):
        response = client.post(path, headers=wrong, json=body)
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


# --- DEC-033: refuse to erase the only remaining admin account; self-erase stays allowed ---
# --- whenever another admin remains (fix block 13-B-F) -------------------------------------


def test_erasing_the_only_remaining_admin_is_refused_and_nothing_is_erased(
    api_env: Engine,
) -> None:
    """Refuses even a SELF-erase once the target is the only live admin account left --
    409, no identifying detail in the message, nothing erased, no deletion request created."""
    _retire_every_live_admin(api_env)
    headers = _admin_headers(api_env, stepped_up=True)
    with Session(api_env) as session:
        remaining = session.scalars(
            select(User).where(User.role == "admin", User.deleted_at.is_(None))
        ).all()
        assert len(remaining) == 1, remaining  # the ONLY live admin, by construction above
        target_id = remaining[0].id
        target_email = remaining[0].email

    client = _client()
    response = client.post(
        "/admin/users/erase",
        json={"email": target_email, "confirmation": "ERASE"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert target_email not in response.text  # no PII in the refusal either

    with Session(api_env) as session:
        reloaded = session.get(User, target_id)
        assert reloaded is not None and reloaded.deleted_at is None
        assert (
            session.scalar(select(DeletionRequest).where(DeletionRequest.user_id == target_id))
            is None
        )
        assert (
            session.scalar(select(AdminAction).where(AdminAction.target == str(target_id))) is None
        )


def test_admin_self_erase_is_allowed_when_another_admin_remains(api_env: Engine) -> None:
    """DEC-033 = A: with (at least) two live admins, self-erase through the admin panel is
    allowed -- the same outcome the self-service ``DELETE /account`` path already permits."""
    _admin_headers(api_env, stepped_up=True)  # another LIVE admin, so the one below is not alone
    email, headers = _admin_session_with_email(api_env, stepped_up=True)
    with Session(api_env) as session:
        target_id = session.scalar(select(User.id).where(User.email == email))
    assert target_id is not None

    client = _client()
    response = client.post(
        "/admin/users/erase",
        json={"email": email, "confirmation": "ERASE"},
        headers=headers,
    )
    assert response.status_code == 202, response.text

    with Session(api_env) as session:
        reloaded = session.get(User, target_id)
        assert reloaded is not None and reloaded.deleted_at is not None
        assert (
            session.scalar(select(DeletionRequest).where(DeletionRequest.user_id == target_id))
            is not None
        )


def test_a_lock_on_an_admin_row_makes_the_last_admin_check_a_retryable_503(
    api_env: Engine,
) -> None:
    """DEC-035 = A (DA-13-BF-1): the last-admin row lock runs under the request path's own
    lock_timeout, so a conflicting lock fails fast as 503 + Retry-After (never hangs the
    worker); nothing is erased, and the same request succeeds once the lock is gone."""
    headers = _admin_headers(api_env, stepped_up=True)
    email, _ = _admin_session_with_email(api_env, stepped_up=True)
    with Session(api_env) as session:
        target_id = session.scalar(select(User.id).where(User.email == email))
    assert target_id is not None

    client = _client()
    with api_env.connect() as holder:
        holder.begin()
        holder.execute(text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": target_id})
        busy = client.post(
            "/admin/users/erase", json={"email": email, "confirmation": "ERASE"}, headers=headers
        )
        holder.rollback()
    assert busy.status_code == 503, busy.text
    assert busy.headers["Retry-After"] == str(ERASURE_RETRY_AFTER_SECONDS)
    with Session(api_env) as session:
        reloaded = session.get(User, target_id)
        assert reloaded is not None and reloaded.deleted_at is None
        assert (
            session.scalar(select(DeletionRequest).where(DeletionRequest.user_id == target_id))
            is None
        )

    ok = client.post(
        "/admin/users/erase", json={"email": email, "confirmation": "ERASE"}, headers=headers
    )
    assert ok.status_code == 202, ok.text


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
