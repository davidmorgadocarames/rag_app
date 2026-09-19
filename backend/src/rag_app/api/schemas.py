"""Request/response models for the API."""

from __future__ import annotations

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
