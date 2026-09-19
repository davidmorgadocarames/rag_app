"""Authentication routes and the current-user dependency."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from rag_app.api.deps import SessionDep
from rag_app.api.schemas import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from rag_app.crypto import generate_user_key, wrap_key
from rag_app.db.models import User, UserKey
from rag_app.erasure import erase_user
from rag_app.security import create_token, decode_token, hash_password, verify_password

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
def register(request: RegisterRequest, session: SessionDep) -> TokenResponse:
    existing = session.scalar(select(User).where(User.email == request.email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    user = User(email=request.email, password_hash=hash_password(request.password))
    session.add(user)
    session.flush()
    session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
    session.commit()
    return TokenResponse(access_token=create_token(str(user.id)))


@router.post("/auth/login", response_model=TokenResponse)
def login(request: LoginRequest, session: SessionDep) -> TokenResponse:
    user = session.scalar(select(User).where(User.email == request.email))
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
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
