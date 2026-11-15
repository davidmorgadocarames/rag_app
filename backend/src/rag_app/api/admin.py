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
and get their own explicit 403 tests instead (T13.1.5/T13.1.8's own Gate line)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select, text, update
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
    rate_limit_account,
)
from rag_app.api.schemas import (
    AdminEraseUserRequest,
    AdminEraseUserResponse,
    AdminErasureResponse,
    AdminEvalResponse,
    AdminFeaturesResponse,
    AdminMetricsResponse,
    ErasureActionResponse,
    ErasureRequestOut,
    EvalMetricOut,
    FeatureHealthOut,
    LastPurgerRunOut,
    MessageResponse,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaEnrollResponse,
    MfaStepUpRequest,
)
from rag_app.authz import require_permission, require_permission_and_mfa
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
    request_erasure,
)
from rag_app.eval.gate import BASELINE_PATH, RESULTS_PATH, THRESHOLDS_PATH
from rag_app.features import REGISTRY, run_health_probe
from rag_app.mfa import hash_recovery_code
from rag_app.purger import run_one_in_background
from rag_app.ratelimit import RateLimiter

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


@router.get(
    "/admin/features",
    response_model=AdminFeaturesResponse,
    tags=["admin"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/features"))),
        Depends(rate_limit_account("admin_features")),
    ],
)
def admin_features(metrics_backend: MetricsBackendDep) -> AdminFeaturesResponse:
    """T13.1.2: list AND detail in the same response (health, metrics summary, enabled
    flags) -- the registry is small enough (``rag_app.features.REGISTRY``) that a separate
    by-id detail route would only add a path-parameterised route the client-tampering
    matrix cannot bare-probe (see this module's docstring) for no real benefit."""
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
            )
        )
    return AdminFeaturesResponse(features=out)


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
def admin_metrics(metrics_backend: MetricsBackendDep) -> AdminMetricsResponse:
    """Every metric name any registry entry declares, queried through ``MetricsBackend``
    (never Prometheus exposed through the BFF directly, PHASE_PLANNING 13.1) -- unlabelled
    counters/gauges only; the latency panel's per-stage histogram is a 13-C concern with its
    own query shape."""
    names = sorted({name for entry in REGISTRY for name in entry.metrics})
    return AdminMetricsResponse(values=metrics_backend.current_values(names))


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
    ],
)
def mfa_enroll(user: CurrentUserDep, session: SessionDep) -> MfaEnrollResponse:
    """(Re-)start enrolment: a fresh, PENDING secret -- MFA is OFF (or off again, if it was
    already on) until ``/account/mfa/confirm`` proves the user can generate a valid code.
    Any earlier recovery codes are invalidated at the same time (they belonged to the old
    secret)."""
    secret = mfa_service.generate_totp_secret()
    record = session.get(UserMfa, user.id)
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
