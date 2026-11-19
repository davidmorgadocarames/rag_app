"""Admin-grade routes: the placeholder ``/admin/ping`` (T12.3.2/T12.3.2b), the admin
dashboard API (T13.1.1-T13.1.8: features, metrics, eval, erasure, erase-account) and MFA
enrolment/confirm/step-up (T12.3.4/T12.3.5).

Every route here resolves through ``rag_app.authz`` (permission map, never a role string
comparison) and, where noted, also requires a fresh MFA step-up on the CURRENT session.
``role`` itself is never writable through any route in this file or anywhere else in the
API -- the admin bootstrap CLI (``rag_app.admin``) is the only writer (T12.3.3).

**Dashboard API routes (DEC-028 A1):** GET -> ``admin:read``, mutating POST -> ``admin:users``
(no new permission); ``/admin/features``, ``/admin/metrics``, ``/admin/eval`` and
``/admin/erasure`` (the list) take no body/path parameter, so they are registered with the
DEFAULT ``matrix=True`` -- the route walker AND the ``PROTECTED_ROUTES`` role x route
tampering matrix (``backend/tests/security/test_client_tampering.py``) cover them with NO
hand-written 403 test (T13.1.1). The three mutating actions below (purge-now, retry,
erase-account) need either a path parameter or a request body a bare matrix probe cannot
supply, so they declare ``matrix=False`` (still covered by the walker -- see
``rag_app.authz.DECLARED_PERMISSION_ROUTES`` -- just not by the generic bare-probe matrix)
and get their own explicit 403 tests instead (T13.1.5/T13.1.8's own Gate line).

**T13.4.1 (SSE, local only):** ``GET /admin/stream`` is also ``matrix=False`` -- not because
it needs a body/path parameter, but because the client-tampering matrix's bare probe
(``client.request(method, path)``) blocks until the response completes, which would hang
forever against this route's open-ended body. Explicit 403 tests cover it instead."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from rag_app import metrics
from rag_app import mfa as mfa_service
from rag_app import sessions as session_service
from rag_app.admin_audit import record_admin_action
from rag_app.api.auth import CurrentUserDep, set_csrf_cookie, set_session_cookie
from rag_app.api.deps import (
    RATE_LIMIT_DEPENDENCIES,
    MetricsBackendDep,
    SessionDep,
    get_rate_limiter,
    get_session_factory,
    rate_limit_account,
)
from rag_app.api.schemas import (
    AdminAlertOut,
    AdminAlertsResponse,
    AdminEraseUserRequest,
    AdminEraseUserResponse,
    AdminErasureResponse,
    AdminEvalResponse,
    AdminFeaturesResponse,
    AdminLatencyResponse,
    AdminMetricsResponse,
    ErasureActionResponse,
    ErasureRequestOut,
    EvalMetricOut,
    FeatureHealthOut,
    LastPurgerRunOut,
    MessageResponse,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaEnrollRequest,
    MfaEnrollResponse,
    MfaStepUpRequest,
    PipelineStageLatencyOut,
    StageLatencyOut,
    StepLatencyOut,
)
from rag_app.authz import ROLE_PERMISSIONS, require_permission, require_permission_and_mfa
from rag_app.config import get_settings
from rag_app.crypto import unwrap_key, wrap_key
from rag_app.csrf import require_csrf
from rag_app.db.models import DeletionRequest, MfaRecoveryCode, PurgerRun, User, UserMfa
from rag_app.erasure import (
    ERASURE_ACCEPTED_MESSAGE,
    ERASURE_RETRY_AFTER_SECONDS,
    ERASURE_RETRYABLE_SQLSTATES,
    OPEN_STATUSES,
    PURGE_DEADLINE_HOURS,
    PURGE_STEPS,
    REQUEST_LOCK_TIMEOUT,
    REQUEST_STATEMENT_TIMEOUT,
    refresh_erasure_gauges,
    request_erasure,
)
from rag_app.eval.gate import BASELINE_PATH, RESULTS_PATH, THRESHOLDS_PATH
from rag_app.features import REGISTRY, run_health_probe
from rag_app.metrics_backend import MetricsBackend
from rag_app.mfa import hash_recovery_code
from rag_app.purger import run_one_in_background
from rag_app.ratelimit import RateLimiter
from rag_app.usage_cap import today_answers

# The eval gate's own metric keys/regression epsilon (rag_app.eval.gate): kept as a local
# constant rather than importing the module-private ``_METRIC_KEYS``/``_REGRESSION_EPSILON``
# so a refactor of that CLI tool's internals cannot silently change what the dashboard shows.
EVAL_METRIC_KEYS = ("retrieval_recall", "faithfulness", "correctness", "correct_abstention")
EVAL_REGRESSION_EPSILON = 0.02

# T13.1.8: the exact phrase an admin must type to erase another account -- never the target's
# email re-typed (already identifies the target; typing it twice would only duplicate PII in
# the request body, not add safety).
ERASE_CONFIRMATION_PHRASE = "ERASE"

router = APIRouter()


# --- T13.4.1: SSE (local) tuning -----------------------------------------------------------
# Code constants (B6 precedent, rag_app.sessions): a connection budget/cadence is a resource
# policy, not an Azure configuration value -- SSE is local-only (T13.4.2's Azure path is
# polling, never this route). Overridable per-test via `app.dependency_overrides[
# get_admin_stream_settings]` (the same pattern as every other `get_*` dependency in this
# codebase), so expiry/idle-close/cap tests run in well under a second.


@dataclass(frozen=True)
class AdminStreamSettings:
    tick_seconds: float = 5.0
    idle_close_seconds: float = 15 * 60
    max_connections: int = 10


def get_admin_stream_settings() -> AdminStreamSettings:
    return AdminStreamSettings()


AdminStreamSettingsDep = Annotated[AdminStreamSettings, Depends(get_admin_stream_settings)]

# A per-PROCESS connection count (Azure never runs this route at all -- T13.4.2 is polling --
# so "per-process" never differs from "per-replica" in practice). Not a prometheus_client
# metric on purpose: an attacker-visible *count* of currently-connected admins is not
# information this dashboard needs to publish, even internally.
_admin_stream_connections = 0


def _rate_limit_mfa(
    user: CurrentUserDep, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    """Bounds TOTP/recovery-code guessing per account, independently of the general
    chat/login limits (T12.3.5). Keyed by user id only here -- the real-IP dimension joins
    once 12b-B's lockout work (T12.4.2) lands alongside this."""
    if not limiter.allow(f"mfa:{user.id}", cost=1.0):
        metrics.RATE_LIMIT_HITS_TOTAL.labels(route_group="mfa").inc()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


RATE_LIMIT_DEPENDENCIES.add(_rate_limit_mfa)


def claim_recovery_code(session: Session, user_id: uuid.UUID, code: str) -> bool:
    """X10 (no TOCTOU, DA-12bA-2/DEBT: see ``docs/security/THREAT_MODEL.md`` X10): claims
    ONE unused recovery code in a SINGLE ``UPDATE ... WHERE used_at IS NULL`` statement --
    never a SELECT to check ``used_at`` followed by a separate UPDATE. Under READ COMMITTED
    (Postgres' default), a second concurrent UPDATE targeting the same row blocks on the
    first's row lock, then re-evaluates its own ``WHERE`` against the row the first call
    already committed -- so at most one caller's ``UPDATE`` ever matches a row, whatever the
    scheduling. A SELECT-then-UPDATE shape would instead let two transactions both read
    ``used_at IS NULL`` before either writes, and both then "claim" the same code.

    Returns whether THIS call is the one that claimed it (``rowcount > 0``). Caller commits
    -- extracted out of the route handler so a concurrency test
    (``test_mfa.py::test_concurrent_claims_of_the_same_recovery_code_let_exactly_one_through``)
    exercises the EXACT statement production uses, not a re-typed copy of it. This is the
    template 12b-B's lockout counters and 12b-C's reset/verify token consume should follow.
    """
    claimed = session.execute(
        update(MfaRecoveryCode)
        .where(
            MfaRecoveryCode.user_id == user_id,
            MfaRecoveryCode.code_hash == hash_recovery_code(code),
            MfaRecoveryCode.used_at.is_(None),
        )
        .values(used_at=dt.datetime.now(dt.UTC))
    )
    return claimed.rowcount > 0


@router.get(
    "/admin/ping",
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/ping"))),
        Depends(rate_limit_account("admin_ping")),
    ],
)
def admin_ping() -> MessageResponse:
    """Placeholder admin route (T12.3.2/T12.3.2b/T12.3.5), proving the permission map + MFA
    step-up gate a real route end to end: an ordinary user -> 403 (no ``admin:read``); an
    admin without step-up -> 403 (MFA required); a stepped-up admin -> 200."""
    return MessageResponse(detail="pong")


# --- T13.0.2/T13.1.2: feature registry (list + detail in one response) --------------------


def _refresh_erasure_gauges(session: Session) -> None:
    """DEBT-046: ``session.get_bind()`` is typed ``Engine | Connection`` -- ``refresh_
    erasure_gauges`` only ever wants the former (same ``isinstance`` guard as its other
    caller, ``rag_app.erasure.request_erasure``); a ``Connection`` (e.g. a test running
    inside one shared transaction) is never seen here since every admin route gets its own
    session off the plain engine-backed factory, but the guard keeps this honest either way
    rather than asserting."""
    bind = session.get_bind()
    if isinstance(bind, Engine):
        refresh_erasure_gauges(bind)


def _features_payload(metrics_backend: MetricsBackend) -> list[FeatureHealthOut]:
    """Shared by the REST route below and the T13.4.1 SSE stream's snapshot/heartbeat (so
    the two never drift into two different shapes of "what a feature card shows")."""
    out = []
    for entry in REGISTRY:
        probe = run_health_probe(entry.health_probe) if entry.health_probe is not None else None
        values = metrics_backend.current_values(entry.metrics) if entry.metrics else {}
        out.append(
            FeatureHealthOut(
                id=entry.id,
                name=entry.name,
                phase=entry.phase,
                status=entry.status,
                probe_status=probe.status if probe is not None else None,
                probe_detail=probe.detail if probe is not None else None,
                metrics=values,
                dev_only=entry.dev_only,
                azure_only=entry.azure_only,
                docs=entry.docs,
            )
        )
    return out


@router.get(
    "/admin/features",
    response_model=AdminFeaturesResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/features"))),
        Depends(rate_limit_account("admin_features")),
    ],
)
def admin_features(
    session: SessionDep, metrics_backend: MetricsBackendDep
) -> AdminFeaturesResponse:
    """T13.1.2: list AND detail in the same response (health, metrics summary, enabled
    flags) -- the registry is small enough (``rag_app.features.REGISTRY``) that a separate
    by-id detail route would only add a path-parameterised route the client-tampering
    matrix cannot bare-probe (see this module's docstring) for no real benefit.

    DEBT-046: refreshes the D-ER erasure gauges from the database on every call, same as
    ``admin_erasure``/``admin_metrics`` below -- see ``refresh_erasure_gauges``'s own
    docstring for why a value the purger Job sets in ITS process never reaches this one
    without a trigger like this.

    ``live_mode`` (T13.4.1/T13.4.2, TF14): the server-side decision the CSR dashboard reads
    to pick SSE vs. polling -- never a frontend build-time variable.

    ``alerts`` (DA-13-E-2): the T13.5.2 banner alerts built from the SAME ``features``
    sweep, so the dashboard's polling tick (Azure) runs the registry probes once, not once
    here and again in ``/admin/alerts``."""
    _refresh_erasure_gauges(session)
    features = _features_payload(metrics_backend)
    return AdminFeaturesResponse(
        features=features,
        live_mode=get_settings().admin_live_mode,
        alerts=_banner_alerts(session, metrics_backend, features),
    )


# --- T13.1.3: MetricsBackend (local), exposed as one admin route --------------------------


@router.get(
    "/admin/metrics",
    response_model=AdminMetricsResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/metrics"))),
        Depends(rate_limit_account("admin_metrics")),
    ],
)
def admin_metrics(session: SessionDep, metrics_backend: MetricsBackendDep) -> AdminMetricsResponse:
    """Every metric name any registry entry declares, queried through ``MetricsBackend``
    (never Prometheus exposed through the BFF directly, PHASE_PLANNING 13.1) -- unlabelled
    counters/gauges only; the latency panel's per-stage histogram is a 13-C concern with its
    own query shape.

    DEBT-046: refreshes the D-ER erasure gauges from the database first -- see
    ``admin_features``'s docstring."""
    _refresh_erasure_gauges(session)
    names = sorted({name for entry in REGISTRY for name in entry.metrics})
    return AdminMetricsResponse(values=metrics_backend.current_values(names))


# --- T13.2.5: latency panel (per-stage p50/p95, in-process -- see metrics.py) --------------


@router.get(
    "/admin/latency",
    response_model=AdminLatencyResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/latency"))),
        Depends(rate_limit_account("admin_latency")),
    ],
)
def admin_latency() -> AdminLatencyResponse:
    """``rag_app.metrics.stage_latency_summary`` reads THIS process's own
    ``secrag_chat_stage_seconds`` histogram directly -- never through ``MetricsBackend``
    (percentiles are not an unlabelled counter/gauge value); the same computation local and
    on Azure, "this replica since it last started", no history (R6-4).

    T13.8.2: ``pipeline`` is the SAME in-process approach applied to the new
    ``secrag_chat_step_seconds`` histogram (``metrics.step_latency_summary``), grouped by UI
    pipeline stage in ``metrics.STEP_ALLOWLIST``'s fixed order -- an unobserved (stage, step)
    pair (nothing has exercised it yet this process) still appears, with ``count=0`` and
    ``None`` averages, so the waterfall's rows never shuffle as traffic arrives."""
    summary = metrics.stage_latency_summary()
    stages = [
        StageLatencyOut(
            stage=stage,
            count=values["count"] or 0.0,
            avg_seconds=values["avg_seconds"],
            p50_seconds=values["p50_seconds"],
            p95_seconds=values["p95_seconds"],
        )
        for stage, values in sorted(summary.items())
    ]
    step_summary = metrics.step_latency_summary()

    def _step_latency(pipeline_stage: str, step: str) -> StepLatencyOut:
        values = step_summary.get((pipeline_stage, step))
        return StepLatencyOut(
            stage=pipeline_stage,
            step=step,
            count=(values["count"] or 0.0) if values else 0.0,
            avg_seconds=values["avg_seconds"] if values else None,
            p50_seconds=values["p50_seconds"] if values else None,
            p95_seconds=values["p95_seconds"] if values else None,
        )

    pipeline = [
        PipelineStageLatencyOut(
            stage=pipeline_stage,
            steps=[_step_latency(pipeline_stage, step) for step in steps],
        )
        for pipeline_stage, steps in metrics.STEP_ALLOWLIST
    ]
    return AdminLatencyResponse(stages=stages, pipeline=pipeline)


# --- T13.1.4: last eval run vs baseline and floors -----------------------------------------


def _read_json(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data: dict[str, float] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _read_results_metrics(path: Path) -> dict[str, float]:
    """``eval/results.json`` is ``{"metrics": {...}, "items": [...]}`` -- a DIFFERENT shape
    from ``_read_json``'s flat ``dict[str, float]`` (baseline/thresholds); this reads only
    the aggregate ``metrics`` sub-dict (T13.1.7: ``items`` -- the per-question golden-set
    text -- is never touched here)."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    metrics = data.get("metrics", {})
    return metrics if isinstance(metrics, dict) else {}


@router.get(
    "/admin/eval",
    response_model=AdminEvalResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/eval"))),
        Depends(rate_limit_account("admin_eval")),
    ],
)
def admin_eval() -> AdminEvalResponse:
    """Aggregate metrics only (T13.1.7): ``eval/results.json``'s per-item golden-set
    question/answer text is never read into the response, only the ``metrics`` summary --
    see ``rag_app.eval.gate`` for where/how that file is written (the ``eval`` gate step)."""
    current = _read_results_metrics(RESULTS_PATH)
    baseline = _read_json(BASELINE_PATH)
    thresholds = _read_json(THRESHOLDS_PATH)
    last_run_at = (
        dt.datetime.fromtimestamp(RESULTS_PATH.stat().st_mtime, tz=dt.UTC)
        if RESULTS_PATH.exists()
        else None
    )
    rows = []
    passed = True
    for key in EVAL_METRIC_KEYS:
        value = current.get(key)
        floor = thresholds.get(key)
        base = baseline.get(key)
        below_floor = value is not None and floor is not None and value < floor
        regressed = (
            value is not None and base is not None and value < base - EVAL_REGRESSION_EPSILON
        )
        if below_floor or regressed:
            passed = False
        rows.append(
            EvalMetricOut(
                key=key,
                value=value,
                floor=floor,
                baseline=base,
                below_floor=below_floor,
                regressed=regressed,
            )
        )
    return AdminEvalResponse(last_run_at=last_run_at, metrics=rows, passed=passed)


# --- T13.1.5: erasure panel (list + purge-now/retry in the background) --------------------


def _rows_remaining(session: Session, user_id: uuid.UUID) -> dict[str, int]:
    return {
        step.name: session.execute(text(step.remaining_count), {"uid": user_id}).scalar_one()
        for step in PURGE_STEPS
    }


def _erasure_payload(session: Session) -> AdminErasureResponse:
    """Shared by the REST route below and the T13.4.1 SSE stream's snapshot/heartbeat."""
    now = dt.datetime.now(dt.UTC)
    rows = session.scalars(
        select(DeletionRequest).order_by(DeletionRequest.requested_at.desc()).limit(500)
    ).all()
    out = []
    for row in rows:
        age = (now - row.requested_at).total_seconds()
        seconds_left = PURGE_DEADLINE_HOURS * 3600 - age
        out.append(
            ErasureRequestOut(
                id=str(row.id),
                requested_at=row.requested_at,
                age_seconds=age,
                seconds_left=seconds_left,
                status=row.status,
                rows_remaining=_rows_remaining(session, row.user_id),
                attempts=row.attempts,
                last_error_class=row.last_error,
                overdue=seconds_left < 0 and row.status != "done",
                completed_at=row.completed_at,
            )
        )
    last_run = session.scalars(
        select(PurgerRun).order_by(PurgerRun.started_at.desc()).limit(1)
    ).first()
    last_run_out = (
        LastPurgerRunOut(
            started_at=last_run.started_at,
            finished_at=last_run.finished_at,
            requests_processed=last_run.requests_processed,
            errors=last_run.errors,
        )
        if last_run is not None
        else None
    )
    return AdminErasureResponse(requests=out, last_purger_run=last_run_out)


@router.get(
    "/admin/erasure",
    response_model=AdminErasureResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/erasure"))),
        Depends(rate_limit_account("admin_erasure")),
    ],
)
def admin_erasure(session: SessionDep) -> AdminErasureResponse:
    """DEBT-046: refreshes the D-ER erasure gauges from the database first -- see
    ``admin_features``'s docstring."""
    _refresh_erasure_gauges(session)
    return _erasure_payload(session)


# --- T13.5.2: banner alerts (R6-1, P13-D4) --------------------------------------------------
#
# Every condition is pre-computed SERVER-SIDE (never a client-side threshold), so the exact
# same business logic covers both live-update modes: the SSE snapshot/heartbeat (local) and
# the `/admin/alerts` REST route below (Azure polling, T13.4.2). Every value read here is
# already available through `MetricsBackend` (in-process on Azure, R6-4), a direct cheap
# query, or the already-computed `features` payload (never a second probe sweep per tick).

ERASURE_ALERT_RED_HOURS = PURGE_DEADLINE_HOURS  # 24h: the purge promise itself (D-ER)
ERASURE_ALERT_AMBER_HOURS = 20  # T13.5.2: an early warning before the 24h promise is missed
# A developer-chosen, documented floor (no history/rate available on Azure, R6-4, so this is
# an absolute count since the replica started, never a rate): more than a handful of
# lockouts in one replica's lifetime is worth an admin's attention.
LOCKOUT_SPIKE_THRESHOLD = 5


def _banner_alerts(
    session: Session, metrics_backend: MetricsBackend, features: list[FeatureHealthOut]
) -> list[AdminAlertOut]:
    """T13.8.3(a): every alert below now carries ``feature_ids`` -- the registry feature(s)
    (``rag_app.features.REGISTRY`` ids) a drawer for that feature should list it under. A
    fixed, reviewed mapping per alert code, except ``probe_failures`` (dynamic: the actual
    failing features' own ids, never a single static guess)."""
    alerts: list[AdminAlertOut] = []

    oldest_open_at = session.scalars(
        select(DeletionRequest.requested_at)
        .where(DeletionRequest.status.in_(OPEN_STATUSES))
        .order_by(DeletionRequest.requested_at.asc())
        .limit(1)
    ).first()
    if oldest_open_at is not None:
        age_hours = (dt.datetime.now(dt.UTC) - oldest_open_at).total_seconds() / 3600
        if age_hours > ERASURE_ALERT_RED_HOURS:
            alerts.append(
                AdminAlertOut(
                    code="erasure_overdue",
                    severity="red",
                    message=(
                        f"An erasure request has been open for over"
                        f" {ERASURE_ALERT_RED_HOURS}h (the purge deadline)."
                    ),
                    feature_ids=["privacy-asynchronous-erasure"],
                )
            )
        elif age_hours > ERASURE_ALERT_AMBER_HOURS:
            alerts.append(
                AdminAlertOut(
                    code="erasure_approaching",
                    severity="amber",
                    message=(
                        f"An erasure request has been open for over"
                        f" {ERASURE_ALERT_AMBER_HOURS}h (deadline: {ERASURE_ALERT_RED_HOURS}h)."
                    ),
                    feature_ids=["privacy-asynchronous-erasure"],
                )
            )

    lockout_hits = metrics_backend.current_value("secrag_lockout_hits_total")
    if lockout_hits is not None and lockout_hits >= LOCKOUT_SPIKE_THRESHOLD:
        alerts.append(
            AdminAlertOut(
                code="lockout_spike",
                severity="amber",
                message=f"Login lockouts: {int(lockout_hits)} since this replica started.",
                feature_ids=["identity-lockout"],
            )
        )

    # dev_only/azure_only entries report "down" BY DESIGN when the feature is off here (e.g.
    # the defence lab, T13.3.1, or an Azure-only check run locally) -- never a real failure.
    failing_probes = [
        f for f in features if f.probe_status == "down" and not f.dev_only and not f.azure_only
    ]
    if failing_probes:
        names = ", ".join(f.name for f in failing_probes)
        alerts.append(
            AdminAlertOut(
                code="probe_failures",
                severity="red",
                message=f"{len(failing_probes)} health probe(s) down: {names}.",
                feature_ids=[f.id for f in failing_probes],
            )
        )

    email_failed = metrics_backend.current_value("secrag_email_failed_total")
    if email_failed is not None and email_failed > 0:
        alerts.append(
            AdminAlertOut(
                code="email_failures",
                severity="amber",
                message=(
                    f"Email delivery failures: {int(email_failed)} since this replica started."
                ),
                feature_ids=["identity-email-verification-reset"],
            )
        )

    cap = get_settings().daily_answer_cap
    if cap > 0:
        answers = today_answers(session)
        if answers >= cap:
            alerts.append(
                AdminAlertOut(
                    code="daily_cap_reached",
                    severity="red",
                    message=f"Daily answer cap reached today ({answers}/{cap}).",
                    feature_ids=["rag-generation-groundedness"],
                )
            )
    return alerts


@router.get(
    "/admin/alerts",
    response_model=AdminAlertsResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/alerts"))),
        Depends(rate_limit_account("admin_alerts")),
    ],
)
def admin_alerts(session: SessionDep, metrics_backend: MetricsBackendDep) -> AdminAlertsResponse:
    """T13.5.2 (R6-1, P13-D4): erasure > 24h (amber > 20h), lockout spikes, probe failures,
    email send failures, daily answer cap reached -- the same ``_banner_alerts`` the SSE
    stream's snapshot/heartbeat also calls (below), so local (SSE) and Azure (polling,
    T13.4.2) show identical alerts for identical underlying state.

    DA-13-E-2: the dashboard itself no longer polls this route -- it reads ``alerts`` from
    ``/admin/features`` (one probe sweep per tick); kept as the stand-alone read for API/CLI
    consumers."""
    _refresh_erasure_gauges(session)
    features = _features_payload(metrics_backend)
    return AdminAlertsResponse(alerts=_banner_alerts(session, metrics_backend, features))


@router.post(
    "/admin/erasure/{request_id}/purge-now",
    response_model=ErasureActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["admin"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission_and_mfa(
                "admin:users",
                route=("POST", "/admin/erasure/{request_id}/purge-now"),
                matrix=False,
            )
        ),
        Depends(rate_limit_account("admin_erasure_action")),
    ],
)
def admin_erasure_purge_now(
    request_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    admin: CurrentUserDep,
    session: SessionDep,
) -> ErasureActionResponse:
    """TF16: runs the purger for ONLY this tombstone in the background (its own engine, see
    ``rag_app.purger.run_one_in_background``) -- resumable if the process stops mid-way, the
    hourly Job finishes it."""
    row = session.get(DeletionRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such erasure request")
    if row.status not in OPEN_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"request is already {row.status}")
    record_admin_action(
        session, admin.id, "erasure_purge_now", target=str(request_id), result="accepted"
    )
    session.commit()
    background_tasks.add_task(run_one_in_background, request_id)
    return ErasureActionResponse(
        detail="Purge started in the background; the hourly job finishes it if this process"
        " stops first.",
        request_id=str(request_id),
    )


@router.post(
    "/admin/erasure/{request_id}/retry",
    response_model=ErasureActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["admin"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission_and_mfa(
                "admin:users", route=("POST", "/admin/erasure/{request_id}/retry"), matrix=False
            )
        ),
        Depends(rate_limit_account("admin_erasure_action")),
    ],
)
def admin_erasure_retry(
    request_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    admin: CurrentUserDep,
    session: SessionDep,
) -> ErasureActionResponse:
    """Same mechanism as "purge now" (``rag_app.purger.Purger.purge_one`` is idempotent and
    resumes from wherever ``progress`` left off regardless of status), but only offered for a
    tombstone the purger already marked ``failed`` -- a distinct route so the UI/CLI action
    matches what actually happened (T13.1.5)."""
    row = session.get(DeletionRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such erasure request")
    if row.status != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, "only a failed request can be retried")
    record_admin_action(
        session, admin.id, "erasure_retry", target=str(request_id), result="accepted"
    )
    session.commit()
    background_tasks.add_task(run_one_in_background, request_id)
    return ErasureActionResponse(
        detail="Retry started in the background; the hourly job finishes it if this process"
        " stops first.",
        request_id=str(request_id),
    )


# --- T13.4.1: SSE (local only) --------------------------------------------------------------


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    # default=str: datetimes (requested_at, last_run.started_at, ...) are not JSON-native --
    # the REST routes above get this for free from FastAPI's own response encoder, this is
    # the SSE generator's equivalent. `default=str` renders them as ISO-ish Python reprs,
    # not quite RFC 3339 -- acceptable for an admin-only diagnostic stream; a polished format
    # would route through the same pydantic ``.model_dump(mode="json")`` as the dict's
    # values already do (see ``_stream_snapshot`` below), which this merely backstops for
    # any other datetime one of them might someday grow.
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream_snapshot(session: Session, metrics_backend: MetricsBackend) -> dict[str, Any]:
    _refresh_erasure_gauges(session)  # DEBT-046, same as every REST route above
    features = _features_payload(metrics_backend)
    return {
        "features": [f.model_dump(mode="json") for f in features],
        "erasure": _erasure_payload(session).model_dump(mode="json"),
        # T13.5.2: computed from the SAME `features` sweep above -- never a second probe run
        # per tick.
        "alerts": [
            a.model_dump(mode="json") for a in _banner_alerts(session, metrics_backend, features)
        ],
    }


async def _admin_stream_events(
    cookie_raw: str, stream_settings: AdminStreamSettings, metrics_backend: MetricsBackend
) -> AsyncIterator[str]:
    """One snapshot immediately, then a "heartbeat" (fresh data, doubling as the
    liveness ping -- TF14/T13.4.1 "snapshot + heartbeat") every ``tick_seconds``.

    Every tick opens a FRESH, short-lived session (never one held open for the stream's
    whole lifetime -- would starve the pool) and re-checks, WITHOUT refreshing
    ``last_seen_at`` (``rag_app.sessions.check_still_valid``, a read-only twin of
    ``authenticate``): the session is still live, the role still grants ``admin:read``, and
    the MFA step-up is still set. Any of the three failing sends a terminal ``closed`` event
    and ends the generator -- the browser's own ``EventSource`` reconnect logic then re-runs
    the full page load, which re-proves all three for real. Idle-closes after
    ``idle_close_seconds`` regardless (bounds one connection's lifetime even if everything
    above keeps checking out)."""
    global _admin_stream_connections
    started = time.monotonic()
    first = True
    try:
        while True:
            if time.monotonic() - started >= stream_settings.idle_close_seconds:
                yield _sse_frame("closed", {"reason": "idle_timeout"})
                return
            factory = get_session_factory()
            with factory() as session:
                user = session_service.check_still_valid(session, cookie_raw)
                if user is None:
                    yield _sse_frame("closed", {"reason": "session_expired"})
                    return
                if "admin:read" not in ROLE_PERMISSIONS.get(user.role, frozenset()):
                    yield _sse_frame("closed", {"reason": "role_lost"})
                    return
                if session_service.mfa_verified_at(session, cookie_raw) is None:
                    yield _sse_frame("closed", {"reason": "mfa_step_up_lost"})
                    return
                payload = _stream_snapshot(session, metrics_backend)
            yield _sse_frame("snapshot" if first else "heartbeat", payload)
            first = False
            await asyncio.sleep(stream_settings.tick_seconds)
    finally:
        _admin_stream_connections -= 1


@router.get(
    "/admin/stream",
    tags=["admin"],
    dependencies=[
        Depends(
            require_permission_and_mfa("admin:read", route=("GET", "/admin/stream"), matrix=False)
        ),
        Depends(rate_limit_account("admin_stream")),
    ],
)
async def admin_stream(
    http: Request,
    stream_settings: AdminStreamSettingsDep,
    metrics_backend: MetricsBackendDep,
) -> StreamingResponse:
    """T13.4.1: SSE, local only -- T13.4.2's Azure path polls the REST routes above instead
    (TF14: which of the two a session uses is decided server-side, see ``admin_features``'s
    ``live_mode`` field, never a frontend build-time variable).

    ``matrix=False`` despite being a bare GET -- see this module's docstring: the
    client-tampering matrix's bare probe blocks on the full response body, which this
    route's open-ended stream would never deliver."""
    cookie_raw = http.cookies.get(session_service.SESSION_COOKIE_NAME)
    if not cookie_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    global _admin_stream_connections
    if _admin_stream_connections >= stream_settings.max_connections:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "too many live admin connections open right now; please try again shortly",
        )
    _admin_stream_connections += 1
    return StreamingResponse(
        _admin_stream_events(cookie_raw, stream_settings, metrics_backend),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- T13.1.8: admin erases another account (DEBT-002) --------------------------------------


@router.post(
    "/admin/users/erase",
    response_model=AdminEraseUserResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["admin"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission_and_mfa(
                "admin:users", route=("POST", "/admin/users/erase"), matrix=False
            )
        ),
        Depends(rate_limit_account("admin_erase_user")),
    ],
)
def admin_erase_user(
    request: AdminEraseUserRequest, admin: CurrentUserDep, session: SessionDep
) -> AdminEraseUserResponse:
    """Email + typed confirmation -> server-side lookup -> the SAME asynchronous erasure as
    self-service (``rag_app.erasure.request_erasure``, exactly what ``DELETE /account``
    calls). The response BODY never reveals how much data existed or anything else about the
    match (T13.1.7); ``admin_actions`` gets a row with the opaque request id as its
    pseudonymised target, never the submitted email.

    **DEC-032 = A (accepted trade-off, documented):** the STATUS CODE does distinguish a
    match from a miss -- 404 on an unmatched email, 202 on a match -- which is a real
    account-existence oracle. Accepted because (a) the actor is already an authenticated,
    MFA-stepped-up admin, not a public probe surface, and (b) the confirmation-phrase check
    above runs BEFORE the lookup, so a match is always ALSO erased on the same request --
    there is no way to silently check many emails without actually deleting every hit, which
    substantially limits real-world abuse of the oracle (see DA-13-B-2 / DEC-032 in
    ``DEBT_REGISTER.md`` for the full analysis).

    **DEC-033 = A:** refuses (409, no identifying detail) to erase the target if it is the
    ONLY remaining non-erased ``role='admin'`` account -- erasing every admin would leave no
    in-API recovery path (only the offline ``rag_app.admin`` CLI with direct DB access).
    Self-erase of an admin account stays ALLOWED whenever at least one OTHER admin remains
    (self-service ``DELETE /account`` already allows an admin to erase themselves, so
    blocking the exact same outcome here too would only be inconsistent UX, not a real
    safety gain) -- the guard only ever counts how many live admin rows are left, never who
    the acting admin is."""
    if request.confirmation != ERASE_CONFIRMATION_PHRASE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type ERASE to confirm")
    target = session.scalar(select(User).where(User.email == request.email))
    if target is None:
        # Authenticated admin action (already MFA-stepped-up), not a public probe surface --
        # see DEC-032 in the docstring above: a direct 404 here is a documented, accepted
        # trade-off, not an oversight.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")
    try:
        if target.role == "admin":
            # DEC-033 = A: row-lock every live admin BEFORE counting and hold the lock for the
            # rest of this transaction (through request_erasure's own commit below). Two admins
            # racing to erase each other therefore serialise on this lock instead of both reading
            # "2 admins left" and leaving zero -- the second one only re-reads the (now smaller)
            # locked set once the first has committed its erasure.
            # DEC-035 = A (DA-13-BF-1): the same bounded waits as request_erasure, set BEFORE
            # this blocking lock, so a conflicting lock (e.g. a migration on users) fails fast
            # as the 503 below instead of hanging the worker.
            session.execute(text(f"SET LOCAL lock_timeout = '{REQUEST_LOCK_TIMEOUT}'"))
            session.execute(text(f"SET LOCAL statement_timeout = '{REQUEST_STATEMENT_TIMEOUT}'"))
            live_admin_ids = session.scalars(
                select(User.id)
                .where(User.role == "admin", User.deleted_at.is_(None))
                .with_for_update()
            ).all()
            if len(live_admin_ids) <= 1:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "cannot erase the only remaining admin account"
                )
        done = request_erasure(session, target.id)
    except OperationalError as exc:
        session.rollback()
        if getattr(exc.orig, "sqlstate", None) in ERASURE_RETRYABLE_SQLSTATES:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The account is busy right now; nothing was deleted. Please try again.",
                headers={"Retry-After": str(ERASURE_RETRY_AFTER_SECONDS)},
            ) from exc
        raise
    if not done:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")
    request_id = session.scalar(
        select(DeletionRequest.id).where(DeletionRequest.user_id == target.id)
    )
    assert request_id is not None  # request_erasure just enqueued it in this same session
    record_admin_action(session, admin.id, "erase_user", target=str(request_id), result="accepted")
    session.commit()
    return AdminEraseUserResponse(detail=ERASURE_ACCEPTED_MESSAGE, request_id=str(request_id))


@router.post(
    "/account/mfa/enroll",
    response_model=MfaEnrollResponse,
    tags=["identity"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission("user:base", route=("POST", "/account/mfa/enroll"), matrix=False)
        ),
        Depends(rate_limit_account("mfa_enroll")),
        # DEBT-051/DEC-041: this route now also checks a TOTP/recovery code (see below), so
        # it shares the same per-account guessing budget as /account/mfa/confirm and
        # /account/mfa/step-up -- not just the general per-route limiter above.
        Depends(_rate_limit_mfa),
    ],
)
def mfa_enroll(
    user: CurrentUserDep, session: SessionDep, request: MfaEnrollRequest | None = None
) -> MfaEnrollResponse:
    """(Re-)start enrolment: a fresh, PENDING secret -- MFA is OFF (or off again, if it was
    already on) until ``/account/mfa/confirm`` proves the user can generate a valid code.
    Any earlier recovery codes are invalidated at the same time (they belonged to the old
    secret).

    DEBT-051/DEC-041: first-time enrolment (no record, or a record that was started but
    never confirmed) is UNCHANGED -- a plain authenticated session is enough, exactly like
    before. Re-enrolling an ALREADY-ENABLED record is different: a session cookie + CSRF
    token alone would let a stolen admin session swap the account's second factor for the
    attacker's and sail straight through step-up next -- found in the 13-P smoke (DEC-041).
    So that path additionally requires a valid CURRENT TOTP code or an unused recovery code
    in the request body, the same proof ``/account/mfa/step-up`` already demands
    (``claim_recovery_code``'s single atomic UPDATE, X10, applies here too -- never a
    SELECT-then-UPDATE)."""
    record = session.get(UserMfa, user.id)
    if record is not None and record.enabled:
        code = request.code if request is not None else None
        verified = code is not None and mfa_service.verify_totp_code(
            unwrap_key(record.secret_encrypted).decode(), code
        )
        if not verified and code is not None:
            # X10 (no TOCTOU): claim_recovery_code is ONE atomic UPDATE -- two concurrent
            # attempts with the same code can never both succeed, and an already-used code
            # can never succeed again.
            verified = claim_recovery_code(session, user.id, code)
        if not verified:
            session.commit()  # persist the (non-)claim attempt either way, same as step-up
            metrics.MFA_REENROLL_TOTAL.labels(result="failure").inc()
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid code")
        metrics.MFA_REENROLL_TOTAL.labels(result="success").inc()

    secret = mfa_service.generate_totp_secret()
    if record is None:
        session.add(UserMfa(user_id=user.id, secret_encrypted=wrap_key(secret.encode())))
    else:
        record.secret_encrypted = wrap_key(secret.encode())
        record.enabled = False
        record.confirmed_at = None
    session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
    session.commit()
    # A live, authenticated user always has an email (an erased account cannot authenticate
    # at all -- rag_app.sessions.authenticate filters deleted_at).
    assert user.email is not None
    uri = mfa_service.provisioning_uri(secret, user.email)
    return MfaEnrollResponse(secret=secret, provisioning_uri=uri)


@router.post(
    "/account/mfa/confirm",
    response_model=MfaConfirmResponse,
    tags=["identity"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission("user:base", route=("POST", "/account/mfa/confirm"), matrix=False)
        ),
        Depends(_rate_limit_mfa),
    ],
)
def mfa_confirm(
    request: MfaConfirmRequest, user: CurrentUserDep, session: SessionDep
) -> MfaConfirmResponse:
    record = session.get(UserMfa, user.id)
    if record is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no pending MFA enrolment")
    secret = unwrap_key(record.secret_encrypted).decode()
    if not mfa_service.verify_totp_code(secret, request.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    record.enabled = True
    record.confirmed_at = dt.datetime.now(dt.UTC)
    session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
    codes = mfa_service.generate_recovery_codes()
    for code in codes:
        session.add(MfaRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code)))
    session.commit()
    return MfaConfirmResponse(recovery_codes=codes)


@router.post(
    "/account/mfa/step-up",
    response_model=MessageResponse,
    tags=["identity"],
    dependencies=[
        Depends(require_csrf),
        Depends(
            require_permission("user:base", route=("POST", "/account/mfa/step-up"), matrix=False)
        ),
        Depends(_rate_limit_mfa),
    ],
)
def mfa_step_up(
    request: MfaStepUpRequest,
    http: Request,
    response: Response,
    user: CurrentUserDep,
    session: SessionDep,
) -> MessageResponse:
    """A valid TOTP code OR an unused recovery code sets ``mfa_verified_at`` on a FRESH
    session (the old session id is rotated away -- T12.3.5) and reissues the session/CSRF
    cookies. Cookie authentication is the only path (T12.5.3): without a session cookie
    ``get_current_user`` already rejects the call with 401, before this body ever runs."""
    record = session.get(UserMfa, user.id)
    if record is None or not record.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA is not enabled on this account")
    cookie_raw = http.cookies.get(session_service.SESSION_COOKIE_NAME)
    if not cookie_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")

    secret = unwrap_key(record.secret_encrypted).decode()
    verified = mfa_service.verify_totp_code(secret, request.code)
    if not verified:
        # X10 (no TOCTOU, docs/security/THREAT_MODEL.md "X10"): claim_recovery_code is ONE
        # atomic UPDATE -- two concurrent attempts with the same code can never both succeed.
        verified = claim_recovery_code(session, user.id, request.code)

    if not verified:
        session.commit()  # persist the (non-)claim attempt either way
        metrics.MFA_STEP_UP_TOTAL.labels(result="failure").inc()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid code")

    new_raw = session_service.rotate(session, cookie_raw)
    if new_raw is None:
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session no longer valid")
    session_service.set_mfa_verified(session, new_raw)
    session.commit()
    set_session_cookie(response, new_raw)
    set_csrf_cookie(response, new_raw)
    metrics.MFA_STEP_UP_TOTAL.labels(result="success").inc()
    return MessageResponse(detail="MFA verified.")
