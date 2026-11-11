"""Authentication routes and the current-user dependency."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from rag_app import lockout
from rag_app import sessions as session_service
from rag_app.api.deps import SessionDep, SignupTrackerDep, rate_limit_login
from rag_app.api.schemas import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResendVerificationResponse,
    TokenResponse,
    UserOut,
    VerifyEmailRequest,
)
from rag_app.config import get_settings
from rag_app.crypto import generate_user_key, wrap_key
from rag_app.csrf import CSRF_COOKIE_NAME, csrf_token, require_csrf, require_origin
from rag_app.db.models import EmailVerificationToken, User, UserKey
from rag_app.emailer import send_already_registered_email, send_verification_email
from rag_app.erasure import (
    ERASURE_ACCEPTED_MESSAGE,
    ERASURE_RETRY_AFTER_SECONDS,
    ERASURE_RETRYABLE_SQLSTATES,
    request_erasure,
)
from rag_app.risk import is_high_risk, signup_risk_score
from rag_app.security import (
    create_token,
    decode_token,
    dummy_password_hash,
    generate_verification_token,
    hash_password,
    hash_token,
    verify_password,
)

# T12.4.4: the SAME response, worded the same way, whatever happened server-side (a brand
# new account, an address that already has one, or a race between two registrations of the
# same address) -- the only way the caller ever learns an address already has an account is
# by actually owning its inbox (send_already_registered_email), never from this response.
GENERIC_REGISTER_MESSAGE = "If that address can be used to register, you'll receive an email."
GENERIC_RESEND_MESSAGE = "If that address needs verifying, you'll receive an email."
EMAIL_VERIFICATION_TOKEN_TTL = dt.timedelta(hours=24)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


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


def _bearer_user(session: SessionDep, token: str) -> User:
    subject = decode_token(token)
    if subject is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
    # An erased account (deleted_at set by the request path) is gone for authentication at
    # once, even while the purger has not removed its row yet: a live token stops working.
    user = session.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return user


def get_current_user(
    session: SessionDep,
    http: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """T12.1.3: the cookie (``__Host-secrag_session``) and the legacy Bearer JWT both
    authenticate — during 12a only, for the pre-BFF frontend's rollback path (TF10). The
    cookie is tried first; when it is present but does not resolve (unknown, idle-expired,
    absolute-expired or revoked — ``rag_app.sessions.authenticate`` does not distinguish
    which, by design), the request is rejected outright rather than silently falling back to
    a Bearer header that might also be present."""
    cookie_raw = http.cookies.get(session_service.SESSION_COOKIE_NAME)
    if cookie_raw:
        user = session_service.authenticate(session, cookie_raw)
        session.commit()
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
        http.state.user_id = user.id  # T12.1.6: rate_limit_chat keys on this, not just the IP
        # T12.3.5: admin-grade routes (rag_app.authz.require_permission_and_mfa) read this --
        # None on the Bearer path below (no server-side session to have stepped up).
        http.state.mfa_verified_at = session_service.mfa_verified_at(session, cookie_raw)
        return user
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    user = _bearer_user(session, credentials.credentials)
    http.state.user_id = user.id
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


@router.post(
    "/auth/register",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_origin)],
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
    yet. See GENERIC_REGISTER_MESSAGE."""
    ip = http.client.host if http.client else "unknown"
    if is_high_risk(signup_risk_score(request.email, tracker.count(ip))):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "signup blocked by risk check")

    existing = session.scalar(select(User).where(User.email == request.email))
    if existing is not None:
        tracker.record(ip)
        send_already_registered_email(request.email)
        return MessageResponse(detail=GENERIC_REGISTER_MESSAGE)

    raw_token = generate_verification_token()
    try:
        user = User(email=request.email, password_hash=hash_password(request.password))
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
    except IntegrityError:
        # A concurrent registration of the same address won the race (double click): the
        # unique index said no. Same generic answer as the "already exists" branch above --
        # never a different one for the racing case, and nothing is logged (the driver's
        # message carries the address in Postgres' DETAIL line, DA-G2-1).
        session.rollback()
        tracker.record(ip)
        send_already_registered_email(request.email)
        return MessageResponse(detail=GENERIC_REGISTER_MESSAGE)

    send_verification_email(request.email, raw_token)
    tracker.record(ip)
    return MessageResponse(detail=GENERIC_REGISTER_MESSAGE)


@router.post("/auth/verify", response_model=MessageResponse, dependencies=[Depends(require_origin)])
def verify_email(request: VerifyEmailRequest, session: SessionDep) -> MessageResponse:
    """T12.4.5: POST, not a GET query param -- the frontend's `/verify?token=...` page (a
    plain GET, safe to pre-fetch) only renders; this is the request its own button click
    issues. A mail scanner that pre-fetches the GET link can never consume the token."""
    now = dt.datetime.now(dt.UTC)
    record = session.scalar(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == hash_token(request.token)
        )
    )
    if record is None or record.used_at is not None or record.expires_at < now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired token")
    # An erased account (deleted_at set; its token rows live until the purge) is not an
    # account any more: no state change, and no row lock the purger would have to wait for
    # (DA-G1-5).
    user = session.scalar(select(User).where(User.id == record.user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid token")
    user.email_verified = True
    record.used_at = now
    session.commit()
    return MessageResponse(detail="Email verified. You can now log in.")


@router.post(
    "/auth/resend-verification",
    response_model=ResendVerificationResponse,
    dependencies=[Depends(require_origin)],
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


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    dependencies=[Depends(require_origin), Depends(rate_limit_login)],
)
def login(request: LoginRequest, session: SessionDep, response: Response) -> TokenResponse:
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
    # T12.1.3: login still returns the JWT (rollback path, TF10) and also opens a session.
    raw_session = session_service.create_session(session, user)
    session.commit()
    set_session_cookie(response, raw_session)
    set_csrf_cookie(response, raw_session)
    return TokenResponse(access_token=create_token(str(user.id)))


@router.post("/auth/logout", response_model=MessageResponse, dependencies=[Depends(require_csrf)])
def logout(http: Request, session: SessionDep, response: Response) -> MessageResponse:
    """T12.1.3/T12.1.4: deletes the session row named by the cookie (a no-op if there is none,
    or if it does not name a live session — logout is idempotent) and always clears both
    cookies. CSRF-protected since 12a-C (``require_csrf``) — a Bearer-only request (no
    session cookie) is exempt, the cookie path needs a matching ``X-CSRF-Token``."""
    cookie_raw = http.cookies.get(session_service.SESSION_COOKIE_NAME)
    if cookie_raw:
        session_service.revoke(session, cookie_raw)
        session.commit()
    _clear_session_cookie(response)
    _clear_csrf_cookie(response)
    return MessageResponse(detail="Logged out.")


@router.get("/auth/me", response_model=UserOut)
def me(user: CurrentUserDep) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, email_verified=user.email_verified)


@router.delete(
    "/account",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf)],
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
