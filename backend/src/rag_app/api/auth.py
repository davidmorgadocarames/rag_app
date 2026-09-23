"""Authentication routes and the current-user dependency."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

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
from rag_app.erasure import erase_user
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
    user = session.scalar(select(User).where(User.id == user_id))
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

    user = User(email=request.email, password_hash=hash_password(request.password))
    session.add(user)
    session.flush()
    session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))

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
    user = session.get(User, record.user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid token")
    user.email_verified = True
    record.used_at = now
    session.commit()
    return MessageResponse(detail="Email verified. You can now log in.")


@router.post("/auth/resend-verification", response_model=ResendVerificationResponse)
def resend_verification(user: CurrentUserDep, session: SessionDep) -> ResendVerificationResponse:
    """Issue a fresh verification token. In dev (no SMTP) return the link to the UI."""
    if user.email_verified:
        return ResendVerificationResponse(detail="Email already verified.")
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
    link = None if get_settings().smtp_host else verification_link(raw_token)
    return ResendVerificationResponse(detail="Verification email sent.", verification_link=link)


@router.post("/auth/login", response_model=TokenResponse, dependencies=[Depends(rate_limit_login)])
def login(request: LoginRequest, session: SessionDep) -> TokenResponse:
    user = session.scalar(select(User).where(User.email == request.email))
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if get_settings().require_email_verification and not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "email not verified")
    return TokenResponse(access_token=create_token(str(user.id)))


@router.get("/auth/me", response_model=UserOut)
def me(user: CurrentUserDep) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, email_verified=user.email_verified)


@router.delete("/account", response_model=MessageResponse)
def delete_account(user: CurrentUserDep, session: SessionDep) -> MessageResponse:
    """GDPR erasure: hard-delete + crypto-shred + tombstone (see docs/adr/0002)."""
    erase_user(session, user.id)
    return MessageResponse(
        detail=(
            "Your account and associated data were erased. Encrypted copies in backups "
            "are put beyond use and deleted within the backup retention window."
        )
    )
