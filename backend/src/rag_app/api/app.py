"""FastAPI application factory and routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rag_app import __version__, metrics, timing
from rag_app.api import auth, conversations
from rag_app.api.auth import get_current_user
from rag_app.api.deps import (
    AnswerFn,
    ReserveFn,
    SessionDep,
    get_answer_reserver,
    get_answerer,
    rate_limit_chat,
)
from rag_app.api.schemas import ChatRequest, ChatResponse, CitationOut, HealthResponse
from rag_app.config import Settings, check_dev_only_flags, get_settings, validate_api_settings
from rag_app.db.session import make_engine
from rag_app.keycheck import check_master_key_fingerprint
from rag_app.logsafe import install_log_redaction

AnswererDep = Annotated[AnswerFn, Depends(get_answerer)]
ReserverDep = Annotated[ReserveFn, Depends(get_answer_reserver)]


def startup_checks(settings: Settings) -> None:
    """Fail closed before serving anything, in this order (API only — never a Job):

    1. settings: DATABASE_URL, JWT_SECRET length, DATA_MASTER_KEY is Fernet, ENV (T11.2.2);
    2. no development-only flag with ENV=prod (T11.0.10);
    3. the master key matches the database's fingerprint, stored on the first start only
       when every existing user key unwraps (T11.2.4, R5-5).
    """
    validate_api_settings(settings)
    check_dev_only_flags(settings)
    engine = make_engine(settings.database_url)
    try:
        check_master_key_fingerprint(engine, settings.data_master_key)
    finally:
        engine.dispose()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start-up checks (``startup_checks``): any failure aborts the start.

    Starts the Prometheus metrics server on its own internal port (T11.3.2) — never on
    this API port — only once the fail-closed checks above have passed.
    """
    settings = get_settings()
    startup_checks(settings)
    metrics.start_metrics_server(settings.metrics_port)
    yield


def create_app() -> FastAPI:
    # Query strings (the e-mail verification token) never reach the access log (T11.2.15).
    install_log_redaction()
    # Per-answer timing JSON lines actually reach stdout (T11.3.1; see install_timing_log).
    timing.install_timing_log()
    app = FastAPI(title="SecRAG API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[get_settings().frontend_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(conversations.router)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    # Authenticated like /chat/stream: every answer costs an LLM call, so anonymous callers
    # must not reach it. Auth runs before the rate limit so they don't consume a bucket.
    @app.post(
        "/chat",
        response_model=ChatResponse,
        dependencies=[Depends(get_current_user), Depends(rate_limit_chat)],
    )
    def chat(
        request: ChatRequest,
        session: SessionDep,
        answerer: AnswererDep,
        reserve: ReserverDep,
    ) -> ChatResponse:
        # Global daily answer cap (R6-1): counted before the LLM call; 429 once reached. In
        # the body, so an unauthenticated, rate-limited or invalid call never uses one up.
        with metrics.track_in_flight():
            reserve(session)
            answer = answerer(session, request.question, request.version)
            return ChatResponse(
                answer=answer.text,
                abstained=answer.abstained,
                grounded=answer.grounded,
                citations=[
                    CitationOut(
                        marker=c.marker,
                        chunk_uid=c.chunk_uid,
                        heading=c.heading,
                        version=c.version,
                        effective_date=c.effective_date,
                    )
                    for c in answer.citations
                ],
            )

    return app


app = create_app()
