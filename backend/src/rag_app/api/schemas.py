"""Request/response models for the API."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    version: str | None = None


class CitationOut(BaseModel):
    marker: int
    chunk_uid: str
    heading: str
    version: str
    effective_date: str | None


class ChatResponse(BaseModel):
    answer: str
    abstained: bool
    grounded: bool
    citations: list[CitationOut]


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    email_verified: bool


class MessageResponse(BaseModel):
    detail: str


class ResendVerificationResponse(BaseModel):
    detail: str
    # Only populated in dev (no SMTP configured), so the UI can offer the link.
    verification_link: str | None = None


class ChatStreamRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    version: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: dt.datetime
    total_tokens: int


class MessageOut(BaseModel):
    role: str
    content: str
    citations: list[CitationOut] = []
    abstained: bool = False
    grounded: bool = True
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: dt.datetime


class ConversationDetailOut(BaseModel):
    id: str
    title: str
    messages: list[MessageOut]


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
