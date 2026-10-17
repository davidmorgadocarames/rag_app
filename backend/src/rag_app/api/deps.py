"""FastAPI dependencies (injectable so endpoints are testable without a live stack)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from rag_app import metrics
from rag_app.client_ip import CLIENT_IP_HEADER, verify_client_ip_header
from rag_app.config import get_settings
from rag_app.db.session import make_session_factory
from rag_app.generation import Answer, answer_question
from rag_app.ratelimit import RateLimiter
from rag_app.reranking import get_shared_reranker
from rag_app.risk import SignupTracker
from rag_app.usage_cap import DAILY_CAP_MESSAGE, reserve_answer, seconds_until_utc_midnight

logger = logging.getLogger("rag_app.api.deps")

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
    # T11.4.1: the ONE process-wide shared reranker (warmed up in the lifespan) — never a
    # fresh load per request.
    def _answer(session: Session, question: str, version: str | None) -> Answer:
        return answer_question(session, question, version=version, reranker=get_shared_reranker())

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


def _connection_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request) -> str:
    """T12.1.6: once ``CLIENT_IP_HMAC_KEYS`` is set, trust only a fresh, correctly-signed
    ``X-SecRAG-Client-IP`` (the BFF derives it from ``X-Forwarded-For``); a missing, forged
    or stale signature falls back to the raw connection address and records a security event
    (a log line + a metrics counter, never the IP itself -- log hygiene). An unconfigured key
    (the feature not adopted yet, 12a-C/D) is the normal path today and emits nothing."""
    keys = [k for k in get_settings().client_ip_hmac_keys.split(",") if k.strip()]
    if not keys:
        return _connection_ip(request)
    header = request.headers.get(CLIENT_IP_HEADER)
    verified = (
        verify_client_ip_header(header, keys, request.method, request.url.path) if header else None
    )
    if verified is not None:
        return verified
    logger.warning("client IP signature missing or invalid; using the connection address")
    metrics.CLIENT_IP_SIGNATURE_EVENTS.inc()
    return _connection_ip(request)


def rate_limit_chat(
    request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    """Keyed by user + real IP (T12.1.6): the authenticated user's id (set by
    ``get_current_user`` on ``request.state.user_id`` -- it always runs before this
    dependency on every chat route) plus the client IP, so one Azure-internal address never
    buckets every user together once the BFF is in front."""
    cost = get_settings().rate_limit_chat_cost
    user_id = getattr(request.state, "user_id", None)
    key = f"chat:{user_id}:{_client_ip(request)}" if user_id else f"chat:{_client_ip(request)}"
    if not limiter.allow(key, cost=cost):
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


def rate_limit_csp_report(
    request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    """``/csp-report`` (T12.2.2, E5): no account to key by (unauthenticated, no session) --
    the real client IP (T12.1.6) alone, so one noisy/hostile reporter cannot drown out
    everyone else's genuine reports either."""
    if not limiter.allow(f"csp-report:{_client_ip(request)}", cost=1.0):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


async def rate_limit_login(
    request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
) -> None:
    """Keyed by account + real IP (T12.1.6): the submitted email (read from the raw body here
    -- NOT a typed ``LoginRequest`` parameter, which would make FastAPI treat this as a
    SECOND body field and embed the JSON contract) plus the client IP. A malformed body
    degrades to IP-only limiting; the endpoint's own body validation still 422s it."""
    email = "unknown"
    try:
        payload = await request.json()
        if isinstance(payload, dict) and isinstance(payload.get("email"), str):
            email = payload["email"]
    except Exception:  # noqa: BLE001 - not JSON / no body: the endpoint's own 422 covers it
        pass
    if not limiter.allow(f"login:{email}:{_client_ip(request)}", cost=1.0):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
