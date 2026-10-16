"""Authentication routes and the current-user dependency."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from rag_app import sessions as session_service
from rag_app.api.deps import SessionDep, SignupTrackerDep, rate_limit_login
from rag_app.api.schemas import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResendVerificationResponse,
    TokenResponse,
    UserOut,
)
from rag_app.config import get_settings
from rag_app.crypto import generate_user_key, wrap_key
from rag_app.csrf import CSRF_COOKIE_NAME, csrf_token, require_csrf, require_origin
from rag_app.db.models import EmailVerificationToken, User, UserKey
from rag_app.emailer import send_verification_email, verification_link
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
    generate_verification_token,
    hash_password,
    hash_token,
    verify_password,
)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


# __Host- cookie attributes (T12.1.3): Secure + Path=/ + no Domain (required by the prefix),
# HttpOnly (never readable by page script) and SameSite=Lax (P12-D2's CSRF layer, 12a-C,
# covers the state-changing routes it still allows cross-site).
def _set_session_cookie(response: Response, raw_session: str) -> None:
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
# no Domain). Issued/rotated together with the session cookie — register and login only in
# 12a (no rotate() call site yet; 12b's step-up wires this in too, T12.3.5).
def _set_csrf_cookie(response: Response, raw_session: str) -> None:
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
        return user
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    user = _bearer_user(session, credentials.credentials)
    http.state.user_id = user.id
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


@router.post(
    "/auth/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
def register(
    request: RegisterRequest,
    http: Request,
    response: Response,
    session: SessionDep,
    tracker: SignupTrackerDep,
) -> TokenResponse:
    ip = http.client.host if http.client else "unknown"
    if is_high_risk(signup_risk_score(request.email, tracker.count(ip))):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "signup blocked by risk check")

    existing = session.scalar(select(User).where(User.email == request.email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

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
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=24),
            )
        )
        # T12.1.3: register opens a session in 12a, same UX as today (the response still
        # carries the JWT too — the rollback path, TF10).
        raw_session = session_service.create_session(session, user)
        session.commit()
    except IntegrityError:
        # A concurrent registration of the same address won the race (double click): the
        # unique index said no. Same answer as the check above, and nothing is logged — the
        # driver's message carries the address in Postgres' DETAIL line (DA-G2-1).
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered") from None

    send_verification_email(request.email, raw_token)
    tracker.record(ip)
    _set_session_cookie(response, raw_session)
    _set_csrf_cookie(response, raw_session)
    return TokenResponse(access_token=create_token(str(user.id)))


@router.get("/auth/verify", response_model=MessageResponse)
def verify_email(token: str, session: SessionDep) -> MessageResponse:
    now = dt.datetime.now(dt.UTC)
    record = session.scalar(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == hash_token(token))
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
    dependencies=[Depends(require_csrf)],
)
def resend_verification(user: CurrentUserDep, session: SessionDep) -> ResendVerificationResponse:
    """Issue a fresh verification token. Only with ENV=dev and no SMTP is the link returned
    to the UI; in prod it never is — without SMTP anyone could otherwise verify an address
    they do not own (DA-G2-5)."""
    if user.email_verified:
        return ResendVerificationResponse(detail="Email already verified.")
    if user.email is None:  # scrubbed by erasure (0005); nothing to send to
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "account has no email address")
    raw_token = generate_verification_token()
    session.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=24),
        )
    )
    session.commit()
    send_verification_email(user.email, raw_token)
    settings = get_settings()
    if settings.smtp_host:
        return ResendVerificationResponse(detail="Verification email sent.")
    if settings.env == "dev":
        return ResendVerificationResponse(
            detail="Verification email sent.", verification_link=verification_link(raw_token)
        )
    return ResendVerificationResponse(detail="Verification email requested.")


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    dependencies=[Depends(require_origin), Depends(rate_limit_login)],
)
def login(request: LoginRequest, session: SessionDep, response: Response) -> TokenResponse:
    user = session.scalar(
        select(User).where(User.email == request.email, User.deleted_at.is_(None))
    )
    if (
        user is None
        or user.password_hash is None  # scrubbed by erasure (0005)
        or not verify_password(request.password, user.password_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if get_settings().require_email_verification and not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "email not verified")
    # T12.1.3: login still returns the JWT (rollback path, TF10) and also opens a session.
    raw_session = session_service.create_session(session, user)
    session.commit()
    _set_session_cookie(response, raw_session)
    _set_csrf_cookie(response, raw_session)
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
