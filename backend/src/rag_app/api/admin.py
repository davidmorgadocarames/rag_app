"""Admin-grade routes: the placeholder ``/admin/ping`` (T12.3.2/T12.3.2b) and MFA
enrolment/confirm/step-up (T12.3.4/T12.3.5).

Every route here resolves through ``rag_app.authz`` (permission map, never a role string
comparison) and, where noted, also requires a fresh MFA step-up on the CURRENT session.
``role`` itself is never writable through any route in this file or anywhere else in the
API -- the admin bootstrap CLI (``rag_app.admin``) is the only writer (T12.3.3).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from rag_app import mfa as mfa_service
from rag_app import sessions as session_service
from rag_app.api.auth import CurrentUserDep, set_csrf_cookie, set_session_cookie
from rag_app.api.deps import SessionDep, get_rate_limiter
from rag_app.api.schemas import (
    MessageResponse,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaEnrollResponse,
    MfaStepUpRequest,
)
from rag_app.authz import require_permission_and_mfa
from rag_app.crypto import unwrap_key, wrap_key
from rag_app.csrf import require_csrf
from rag_app.db.models import MfaRecoveryCode, UserMfa
from rag_app.mfa import hash_recovery_code
from rag_app.ratelimit import RateLimiter

router = APIRouter()


def _rate_limit_mfa(
    user: CurrentUserDep, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    """Bounds TOTP/recovery-code guessing per account, independently of the general
    chat/login limits (T12.3.5). Keyed by user id only here -- the real-IP dimension joins
    once 12b-B's lockout work (T12.4.2) lands alongside this."""
    if not limiter.allow(f"mfa:{user.id}", cost=1.0):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


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
    dependencies=[Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/ping")))],
)
def admin_ping() -> MessageResponse:
    """Placeholder admin route (T12.3.2/T12.3.2b/T12.3.5), proving the permission map + MFA
    step-up gate a real route end to end: an ordinary user -> 403 (no ``admin:read``); an
    admin without step-up -> 403 (MFA required); a stepped-up admin -> 200."""
    return MessageResponse(detail="pong")


@router.post(
    "/account/mfa/enroll",
    response_model=MfaEnrollResponse,
    dependencies=[Depends(require_csrf)],
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
    dependencies=[Depends(require_csrf), Depends(_rate_limit_mfa)],
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
    dependencies=[Depends(require_csrf), Depends(_rate_limit_mfa)],
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid code")

    new_raw = session_service.rotate(session, cookie_raw)
    if new_raw is None:
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session no longer valid")
    session_service.set_mfa_verified(session, new_raw)
    session.commit()
    set_session_cookie(response, new_raw)
    set_csrf_cookie(response, new_raw)
    return MessageResponse(detail="MFA verified.")
