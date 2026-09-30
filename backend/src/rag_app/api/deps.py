"""FastAPI dependencies (injectable so endpoints are testable without a live stack)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from rag_app.config import get_settings
from rag_app.db.session import make_session_factory
from rag_app.generation import Answer, answer_question
from rag_app.ratelimit import RateLimiter
from rag_app.risk import SignupTracker
from rag_app.usage_cap import DAILY_CAP_MESSAGE, reserve_answer, seconds_until_utc_midnight

AnswerFn = Callable[[Session, str, str | None], Answer]

_session_factory = None
_rate_limiter: RateLimiter | None = None
_signup_tracker = SignupTracker()


def get_session() -> Iterator[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    with _session_factory() as session:
        yield session


def get_answerer() -> AnswerFn:
    def _answer(session: Session, question: str, version: str | None) -> Answer:
        return answer_question(session, question, version=version)

    return _answer


SessionDep = Annotated[Session, Depends(get_session)]


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        settings = get_settings()
        _rate_limiter = RateLimiter(
            settings.rate_limit_capacity, settings.rate_limit_refill_per_second
        )
    return _rate_limiter


def get_signup_tracker() -> SignupTracker:
    return _signup_tracker


SignupTrackerDep = Annotated[SignupTracker, Depends(get_signup_tracker)]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit_chat(
    request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    cost = get_settings().rate_limit_chat_cost
    if not limiter.allow(f"chat:{_client_ip(request)}", cost=cost):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


ReserveFn = Callable[[Session], None]


def reserve_daily_answer(session: Session) -> None:
    """``/chat``: count the answer in ``usage_daily`` before the LLM is called (R6-1,
    DA-G3-1); 429 with ``Retry-After`` (seconds to the next UTC midnight) once the global
    daily cap is reached. ``/chat`` has no chit-chat fast path, so every call counts.

    Called from the handler body, not as a dependency: dependencies run before the request
    body is validated, and an invalid request must not use up an answer."""
    if not reserve_answer(session, get_settings().daily_answer_cap):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            DAILY_CAP_MESSAGE,
            headers={"Retry-After": str(seconds_until_utc_midnight())},
        )


def get_answer_reserver() -> ReserveFn:
    return reserve_daily_answer


def rate_limit_login(
    request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    if not limiter.allow(f"login:{_client_ip(request)}", cost=1.0):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
