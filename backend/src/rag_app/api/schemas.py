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
