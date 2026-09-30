"""Authentication routes and the current-user dependency."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

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


def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    subject = decode_token(credentials.credentials)
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


CurrentUserDep = Annotated[User, Depends(get_current_user)]


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: RegisterRequest,
    http: Request,
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
        session.commit()
    except IntegrityError:
        # A concurrent registration of the same address won the race (double click): the
        # unique index said no. Same answer as the check above, and nothing is logged — the
        # driver's message carries the address in Postgres' DETAIL line (DA-G2-1).
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered") from None

    send_verification_email(request.email, raw_token)
    tracker.record(ip)
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


@router.post("/auth/resend-verification", response_model=ResendVerificationResponse)
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


@router.post("/auth/login", response_model=TokenResponse, dependencies=[Depends(rate_limit_login)])
def login(request: LoginRequest, session: SessionDep) -> TokenResponse:
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
    return TokenResponse(access_token=create_token(str(user.id)))


@router.get("/auth/me", response_model=UserOut)
def me(user: CurrentUserDep) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, email_verified=user.email_verified)


@router.delete("/account", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
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
