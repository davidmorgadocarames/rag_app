"""Authentication routes and the current-user dependency."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from rag_app import lockout
from rag_app import sessions as session_service
from rag_app.api.deps import (
    SessionDep,
    SignupTrackerDep,
    rate_limit_account,
    rate_limit_email,
    rate_limit_login,
)
from rag_app.api.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResendVerificationResponse,
    ResetPasswordRequest,
    UserOut,
    VerifyEmailRequest,
)
from rag_app.config import get_settings
from rag_app.crypto import generate_user_key, wrap_key
from rag_app.csrf import CSRF_COOKIE_NAME, csrf_token, require_csrf, require_origin
from rag_app.db.models import EmailVerificationToken, PasswordResetToken, User, UserKey
from rag_app.emailer import (
    send_already_registered_email,
    send_password_reset_email,
    send_password_reset_unknown_account_email,
    send_verification_email,
)
from rag_app.erasure import (
    ERASURE_ACCEPTED_MESSAGE,
    ERASURE_RETRY_AFTER_SECONDS,
    ERASURE_RETRYABLE_SQLSTATES,
    request_erasure,
)
from rag_app.password_policy import validate_password
from rag_app.risk import is_high_risk, signup_risk_score
from rag_app.security import (
    dummy_password_hash,
    generate_verification_token,
    hash_password,
    hash_token,
    verify_password,
)

logger = logging.getLogger("rag_app.api.auth")

# T12.4.4: the SAME response, worded the same way, whatever happened server-side (a brand
# new account, an address that already has one, or a race between two registrations of the
# same address) -- the only way the caller ever learns an address already has an account is
# by actually owning its inbox (send_already_registered_email), never from this response.
GENERIC_REGISTER_MESSAGE = "If that address can be used to register, you'll receive an email."
GENERIC_RESEND_MESSAGE = "If that address needs verifying, you'll receive an email."
# T12.4.6: the forgot-password request is enumeration-safe the same way (F3/F11) -- whether
# the address has an account, is unverified, or does not exist, the caller sees this SAME
# message; only the real owner of the address, by its inbox, learns anything (or nothing, if
# they never asked).
GENERIC_FORGOT_PASSWORD_MESSAGE = (
    "If that address has an account, you'll receive a password reset email."
)
EMAIL_VERIFICATION_TOKEN_TTL = dt.timedelta(hours=24)
PASSWORD_RESET_TOKEN_TTL = dt.timedelta(minutes=30)

# T12.4.6 DA re-check: the `users_email_key` unique constraint on `users.email` (0002,
# `email varchar NOT NULL UNIQUE`) -- Postgres' own default name for an inline `UNIQUE`
# constraint on that column. This is the ONLY `IntegrityError` register's `except` clause
# below treats as "this address already has an account"; the DB driver (psycopg 3) exposes
# both the SQLSTATE and the violated constraint's name on `exc.orig`.
_DUPLICATE_EMAIL_SQLSTATE = "23505"  # unique_violation
_DUPLICATE_EMAIL_CONSTRAINT = "users_email_key"

router = APIRouter()


def _is_duplicate_email_conflict(exc: IntegrityError) -> bool:
    """T12.4.6 DA re-check: register's three inserts (``User``, ``UserKey``,
    ``EmailVerificationToken``) used to treat ANY ``IntegrityError`` from any one of them as
    "this email already has an account" -- a NOT NULL/FK violation on ``user_keys`` or
    ``email_verification_tokens`` would have silently looked exactly like a normal
    registration race and returned the generic 202, hiding a real bug behind the
    enumeration-safe response. Only the `users.email` unique-constraint violation (this exact
    SQLSTATE AND constraint name) is that case -- see
    ``test_register_reraises_an_integrity_error_that_is_not_the_email_conflict``."""
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None) if diag is not None else None
    return sqlstate == _DUPLICATE_EMAIL_SQLSTATE and constraint == _DUPLICATE_EMAIL_CONSTRAINT


# __Host- cookie attributes (T12.1.3): Secure + Path=/ + no Domain (required by the prefix),
# HttpOnly (never readable by page script) and SameSite=Lax (P12-D2's CSRF layer, 12a-C,
# covers the state-changing routes it still allows cross-site).
def set_session_cookie(response: Response, raw_session: str) -> None:
    response.set_cookie(
        session_service.SESSION_COOKIE_NAME,
        raw_session,
        path=session_service.COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        session_service.SESSION_COOKIE_NAME,
        path=session_service.COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


# CSRF double-submit cookie (T12.1.4, P12-D2): readable (NOT HttpOnly) so client-side
# JavaScript can echo its value in the X-CSRF-Token header; still __Host- (Secure + Path=/ +
# no Domain). Issued/rotated together with the session cookie — register and login in 12a;
# the MFA step-up's session rotation (``rag_app.api.admin``, T12.3.5) also reissues both.
def set_csrf_cookie(response: Response, raw_session: str) -> None:
    settings = get_settings()
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token(settings.csrf_secret, raw_session),
        path=session_service.COOKIE_PATH,
        httponly=False,
        secure=True,
        samesite="lax",
    )


def _clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path=session_service.COOKIE_PATH,
        httponly=False,
        secure=True,
        samesite="lax",
    )


def get_current_user(session: SessionDep, http: Request) -> User:
    """T12.1.3 + T12.5.3: the ``__Host-secrag_session`` cookie is the ONLY way to
    authenticate -- the legacy Bearer JWT path (the pre-BFF frontend's 12a rollback, TF10)
    is gone; ``/auth/login`` no longer issues or returns a token at all (``create_token``/
    ``decode_token`` stay in ``rag_app.security`` as plain, independently-tested utility
    functions -- full removal is T15.2.5, see the module they live in).

    No cookie at all, or one that does not resolve (unknown, idle-expired, absolute-expired
    or revoked -- ``rag_app.sessions.authenticate`` does not distinguish which, by design) —
    both are rejected the same way, a plain 401."""
    cookie_raw = http.cookies.get(session_service.SESSION_COOKIE_NAME)
    if cookie_raw is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    user = session_service.authenticate(session, cookie_raw)
    session.commit()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
    http.state.user_id = user.id  # T12.1.6: rate_limit_chat keys on this, not just the IP
    # T12.3.5: admin-grade routes (rag_app.authz.require_permission_and_mfa) read this.
    http.state.mfa_verified_at = session_service.mfa_verified_at(session, cookie_raw)
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


@router.post(
    "/auth/register",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_origin), Depends(rate_limit_email("register"))],
)
def register(
    request: RegisterRequest,
    http: Request,
    session: SessionDep,
    tracker: SignupTrackerDep,
) -> MessageResponse:
    """T12.4.4: a generic 202 whatever the outcome (new account, an address that already
    has one, or a race between two registrations of the same address) -- never a 409, and
    NO session/token on success any more (F3/F11, user enumeration): the account must be
    verified by email before it can do anything, so there is nothing useful to log in as
    yet. See GENERIC_REGISTER_MESSAGE.

    DA-12bB-1 (F3/OWASP-A07, the TIMING half of user enumeration): the "already registered"
    and "brand new account" outcomes must cost the same, not just look the same. There is no
    upfront "does this email exist" SELECT any more -- every call pays the argon2 hash
    unconditionally and ALWAYS attempts the same three inserts + flush + commit; an address
    that already has an account only differs in that the unique index on ``users.email``
    rejects the insert (``IntegrityError``, same branch a genuine registration race already
    used), discarded by a rollback rather than detected in advance by a cheaper read. The two
    outcomes are now the literal same code path up to that point, never a cheaper one for an
    attacker to time against (``test_register_timing_does_not_distinguish_an_existing_account_
    from_a_new_one``)."""
    ip = http.client.host if http.client else "unknown"
    if is_high_risk(signup_risk_score(request.email, tracker.count(ip))):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "signup blocked by risk check")

    # Paid on EVERY call, whatever the outcome turns out to be below -- same reasoning as
    # login's dummy_password_hash (T12.4.1): an attacker must not be able to time-distinguish
    # "already registered" from "new account" by the presence/absence of this argon2 call.
    password_hash = hash_password(request.password)
    raw_token = generate_verification_token()
    try:
        user = User(email=request.email, password_hash=password_hash)
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
        session.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) + EMAIL_VERIFICATION_TOKEN_TTL,
            )
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if not _is_duplicate_email_conflict(exc):
            # T12.4.6 DA re-check: anything OTHER than the users.email unique-constraint
            # violation (e.g. a NOT NULL/FK problem on user_keys or
            # email_verification_tokens) is a real bug, not a registration race -- it must
            # surface as a 500, never be swallowed into the enumeration-safe generic
            # response. The exception type name only (never str(exc), which could echo the
            # address back via Postgres' DETAIL line, DA-G2-1) -- no PII in this log record.
            logger.error(
                "register: unexpected integrity error, not the email-conflict case (%s)",
                type(exc).__name__,
            )
            raise
        # The address already has an account, OR a concurrent registration of it just won
        # the race (double click) -- either way the unique index said no, discovered only
        # now (never by a cheaper upfront read), and the response plus the email sent are
        # the same as the race case always used. Nothing is logged (the driver's message
        # carries the address in Postgres' DETAIL line, DA-G2-1).
        tracker.record(ip)
        send_already_registered_email(request.email)
        return MessageResponse(detail=GENERIC_REGISTER_MESSAGE)

    send_verification_email(request.email, raw_token)
    tracker.record(ip)
    return MessageResponse(detail=GENERIC_REGISTER_MESSAGE)


def _consume_email_verification_token(
    session: Session, raw_token: str, now: dt.datetime
) -> uuid.UUID | None:
    """X10 (no TOCTOU, docs/security/THREAT_MODEL.md "X10", REC-04/REC-05 template): ONE
    atomic ``UPDATE ... WHERE used_at IS NULL AND expires_at > :now`` statement, never a
    SELECT to check ``used_at``/``expires_at`` followed by a separate UPDATE. The owning
    account must also still be live (not erased) -- checked IN THE SAME statement via a
    correlated subquery against ``users.deleted_at``, so an erased account's leftover token
    row (kept until the purger's next pass) can never be "verified" by a race with the
    purge. Returns the claimed ``user_id``, or ``None`` if nothing matched."""
    return session.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.token_hash == hash_token(raw_token),
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.expires_at > now,
            EmailVerificationToken.user_id.in_(select(User.id).where(User.deleted_at.is_(None))),
        )
        .values(used_at=now)
        .returning(EmailVerificationToken.user_id)
    ).scalar()


@router.post("/auth/verify", response_model=MessageResponse, dependencies=[Depends(require_origin)])
def verify_email(request: VerifyEmailRequest, session: SessionDep) -> MessageResponse:
    """T12.4.5: POST, not a GET query param -- the frontend's `/verify?token=...` page (a
    plain GET, safe to pre-fetch) only renders; this is the request its own button click
    issues. A mail scanner that pre-fetches the GET link can never consume the token.

    T12.4.6 (X10): the token consume is now the SAME atomic-UPDATE shape as the reset token
    below and ``rag_app.api.admin.claim_recovery_code`` -- see
    ``_consume_email_verification_token``."""
    now = dt.datetime.now(dt.UTC)
    user_id = _consume_email_verification_token(session, request.token, now)
    if user_id is None:
        session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired token")
    session.execute(update(User).where(User.id == user_id).values(email_verified=True))
    session.commit()
    return MessageResponse(detail="Email verified. You can now log in.")


@router.post(
    "/auth/resend-verification",
    response_model=ResendVerificationResponse,
    dependencies=[Depends(require_origin), Depends(rate_limit_email("resend"))],
)
def resend_verification(
    request: ResendVerificationRequest, session: SessionDep
) -> ResendVerificationResponse:
    """T12.4.4: reached by email, not by an authenticated session -- a freshly registered
    (unverified) account has no session any more (no session on register), so it could
    never call an authenticated resend. Same generic response whatever the outcome (no
    account, already verified, or a fresh token actually issued): see GENERIC_RESEND_MESSAGE.
    The link never travels in this response, in any environment (T12.1.11) -- only by email
    (Mailpit locally, DA-G2-5): without SMTP anyone could otherwise verify an address they do
    not own."""
    user = session.scalar(
        select(User).where(User.email == request.email, User.deleted_at.is_(None))
    )
    if user is not None and not user.email_verified and user.email is not None:
        raw_token = generate_verification_token()
        session.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) + EMAIL_VERIFICATION_TOKEN_TTL,
            )
        )
        session.commit()
        send_verification_email(user.email, raw_token)
    return ResendVerificationResponse(detail=GENERIC_RESEND_MESSAGE)


def _consume_password_reset_token(
    session: Session, raw_token: str, now: dt.datetime
) -> uuid.UUID | None:
    """X10 (no TOCTOU): the exact same shape as ``_consume_email_verification_token``
    above, for ``password_reset_tokens`` (0007 "part 3", T12.4.6) -- ONE atomic
    ``UPDATE ... WHERE used_at IS NULL AND expires_at > :now`` (plus the live-account
    subquery), never a SELECT-then-UPDATE. See
    ``test_concurrent_resets_with_the_same_token_let_exactly_one_through`` and
    ``test_consume_password_reset_token_is_a_single_atomic_statement``."""
    return session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == hash_token(raw_token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
            PasswordResetToken.user_id.in_(select(User.id).where(User.deleted_at.is_(None))),
        )
        .values(used_at=now)
        .returning(PasswordResetToken.user_id)
    ).scalar()


@router.post(
    "/auth/forgot-password",
    response_model=MessageResponse,
    dependencies=[Depends(require_origin), Depends(rate_limit_email("forgot"))],
)
def forgot_password(request: ForgotPasswordRequest, session: SessionDep) -> MessageResponse:
    """T12.4.6: a generic response whatever the outcome (no account, an unverified/erased
    account, or a fresh reset token actually issued) -- the SAME enumeration-safe discipline
    as register (F3/F11, GENERIC_FORGOT_PASSWORD_MESSAGE). Unlike register, there is no
    unique-index trick to make the DB write itself unconditional (a reset token needs a
    REAL user_id to satisfy its foreign key) -- cost parity instead comes from ALWAYS
    sending exactly one email via the same ``_send`` call either way: the real reset link to
    the real owner, or a harmless "no account uses this address" notice to whoever
    submitted an address with no live, verified account. The only remaining difference is
    one small extra INSERT+commit on the real-account branch, well inside the timing test's
    tolerance (see test_forgot_password_timing_does_not_distinguish_an_existing_account_
    from_an_unknown_one)."""
    user = session.scalar(
        select(User).where(User.email == request.email, User.deleted_at.is_(None))
    )
    if user is not None and user.email_verified and user.email is not None:
        raw_token = generate_verification_token()
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=dt.datetime.now(dt.UTC) + PASSWORD_RESET_TOKEN_TTL,
            )
        )
        session.commit()
        send_password_reset_email(user.email, raw_token)
    else:
        send_password_reset_unknown_account_email(request.email)
    return MessageResponse(detail=GENERIC_FORGOT_PASSWORD_MESSAGE)


@router.post("/auth/reset", response_model=MessageResponse, dependencies=[Depends(require_origin)])
def reset_password(request: ResetPasswordRequest, session: SessionDep) -> MessageResponse:
    """T12.4.6: POST with the token in the body (same discipline as ``/auth/verify``) --
    atomic consume (X10, ``_consume_password_reset_token``). Success sets the new password,
    revokes EVERY session of the account (logout-everywhere: a password reset is at least as
    strong a signal as a fresh login, and must invalidate any session an attacker who had the
    OLD password might already be holding) and clears any login lockout (proving account
    ownership this way is as good as a correct login, T12.4.2's ``clear_lockout``)."""
    now = dt.datetime.now(dt.UTC)
    token_hash = hash_token(request.token)
    # A read-only lookup first (NOT the claim -- that is the atomic consume below, which
    # re-checks used_at/expires_at itself): finds which live account this token belongs to,
    # so the full password policy (T12.4.3, including "must not contain the email local
    # part", not knowable at the schema layer before the token resolves to a user) can
    # reject a bad password WITHOUT burning the single-use token on a request that would
    # fail anyway. Not TOCTOU-sensitive (nothing is mutated based on this read) -- same
    # reasoning as rag_app.lockout.locked_until's own cheap read.
    pending = session.execute(
        select(User.email)
        .join(PasswordResetToken, PasswordResetToken.user_id == User.id)
        .where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if pending is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired token")
    problems = validate_password(request.password, pending or "")
    if problems:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(problems))

    user_id = _consume_password_reset_token(session, request.token, now)
    if user_id is None:
        session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired token")
    user = session.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:  # erased in the brief window between the two statements above (rare)
        session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid token")
    user.password_hash = hash_password(request.password)
    session_service.revoke_all(session, user.id)
    if user.email is not None:
        lockout.clear_lockout(session, get_settings().lockout_hmac_secret, user.email)
    session.commit()
    return MessageResponse(detail="Password has been reset. Please log in again.")


@router.post(
    "/auth/login",
    response_model=MessageResponse,
    dependencies=[Depends(require_origin), Depends(rate_limit_login)],
)
def login(request: LoginRequest, session: SessionDep, response: Response) -> MessageResponse:
    """T12.4.1 (constant-time, F3 user enumeration via timing) + T12.4.2 (lockout, F5/API-6).

    A dummy argon2 verify runs on EVERY failing path (no such email, no password hash, or a
    real but wrong one) so the argon2 cost is paid identically whether the email exists or
    not (``rag_app.security.dummy_password_hash``). The lockout counter (X10, one atomic
    statement, ``rag_app.lockout``) is kept BEFORE the email-verification gate so a correct
    password always clears it, but only incremented on an actually wrong credential --
    never on a correct-password-but-unverified attempt, which proves nothing malicious."""
    settings = get_settings()
    lock = lockout.locked_until(session, settings.lockout_hmac_secret, request.email)
    if lock is not None and lock > dt.datetime.now(dt.UTC):
        retry_after = max(1, int((lock - dt.datetime.now(dt.UTC)).total_seconds()))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed attempts; account temporarily locked",
            headers={"Retry-After": str(retry_after)},
        )

    user = session.scalar(
        select(User).where(User.email == request.email, User.deleted_at.is_(None))
    )
    password_hash = (
        user.password_hash
        if user is not None and user.password_hash is not None
        else dummy_password_hash()
    )
    valid = verify_password(request.password, password_hash)
    if user is None or user.password_hash is None or not valid:
        lockout.record_failed_login(session, settings.lockout_hmac_secret, request.email)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if settings.require_email_verification and not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "email not verified")

    lockout.clear_lockout(session, settings.lockout_hmac_secret, request.email)
    # T12.5.3: login no longer returns a JWT -- only the session + CSRF cookies.
    raw_session = session_service.create_session(session, user)
    session.commit()
    set_session_cookie(response, raw_session)
    set_csrf_cookie(response, raw_session)
    return MessageResponse(detail="Logged in.")


@router.post("/auth/logout", response_model=MessageResponse, dependencies=[Depends(require_csrf)])
def logout(http: Request, session: SessionDep, response: Response) -> MessageResponse:
    """T12.1.3/T12.1.4: deletes the session row named by the cookie (a no-op if there is none,
    or if it does not name a live session — logout is idempotent) and always clears both
    cookies. CSRF-protected since 12a-C (``require_csrf``) — a call with no session cookie at
    all (already logged out, or never logged in) is exempt: there is nothing to protect, the
    cookie path needs a matching ``X-CSRF-Token``."""
    cookie_raw = http.cookies.get(session_service.SESSION_COOKIE_NAME)
    if cookie_raw:
        session_service.revoke(session, cookie_raw)
        session.commit()
    _clear_session_cookie(response)
    _clear_csrf_cookie(response)
    return MessageResponse(detail="Logged out.")


@router.get(
    "/auth/me",
    response_model=UserOut,
    dependencies=[Depends(rate_limit_account("auth_me"))],
)
def me(user: CurrentUserDep) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, email_verified=user.email_verified)


@router.delete(
    "/account",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf), Depends(rate_limit_account("delete_account"))],
)
def delete_account(user: CurrentUserDep, session: SessionDep) -> MessageResponse:
    """GDPR erasure, asynchronous (D-ER, T11.2b.2): one short transaction deletes the data key
    (crypto-shred), scrubs the email and password hash, marks the account deleted and queues
    the tombstone; the batched purger removes the remaining rows within 24 h. 202 Accepted.

    See docs/adr/adr_phase06_gdpr_erasure.md and docs/adr/adr_phase11_stability.md.
    """
    try:
        request_erasure(session, user.id)
    except OperationalError as exc:
        # The short transaction hit its lock/statement timeout (another transaction holds the
        # user's row): nothing was changed (rolled back) — a retryable 503, not a 500 (DA-G1-4).
        session.rollback()
        if getattr(exc.orig, "sqlstate", None) in ERASURE_RETRYABLE_SQLSTATES:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The account is busy right now; nothing was deleted. Please try again.",
                headers={"Retry-After": str(ERASURE_RETRY_AFTER_SECONDS)},
            ) from exc
        raise
    return MessageResponse(detail=ERASURE_ACCEPTED_MESSAGE)
