"""FastAPI application factory and routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from rag_app import __version__, csp_report, metrics, reranking, timing
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
from rag_app.csrf import require_csrf
from rag_app.db.session import make_engine
from rag_app.keycheck import check_master_key_fingerprint
from rag_app.logsafe import install_log_redaction

# T12.2.4 (E6): the old frontend (12a rollback, TF10) is the only remaining *browser* CORS
# caller -- it sends a Bearer token (never cookies), so `allow_credentials` stays False and
# the method/header lists only need to cover what it actually uses. The BFF-based new
# frontend never triggers a browser CORS preflight at all (same-origin, server-to-server
# fetch) -- `test_cors_narrowed_to_the_old_frontends_needs` pins both halves of this.
CORS_ALLOWED_METHODS = ["GET", "POST", "PATCH", "DELETE"]
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type"]

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
    this API port — only once the fail-closed checks above have passed. Then loads and
    warms up the ONE shared cross-encoder reranker (T11.4.1): the first real chat request
    pays no model-load/first-inference cost, because it is already resident by the time
    this coroutine yields and the app starts serving traffic.
    """
    settings = get_settings()
    startup_checks(settings)
    metrics.start_metrics_server(settings.metrics_port, addr=settings.metrics_bind_addr)
    reranking.get_shared_reranker().warm_up()
    yield


def _add_security_headers(app: FastAPI) -> None:
    """T12.2.4 (E6): the same handful of headers on every API response, never only on the
    "interesting" routes -- a response that never reaches this middleware does not exist
    (FastAPI/Starlette run ``http`` middleware around every route, including 404s).

    ``frame-ancestors 'none'`` is carried in a ``Content-Security-Policy`` header rather
    than the legacy ``X-Frame-Options`` (same effect, the modern directive): this API is
    never meant to render inside a frame, on any origin including its own. ``Cache-Control:
    no-store`` on every response is deliberately broad ("where sensible" = everywhere on an
    authenticated JSON API that only ever returns session-specific or already up-to-date
    data -- nothing here benefits from a shared/browser cache)."""

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app() -> FastAPI:
    # Query strings (the e-mail verification token) never reach the access log (T11.2.15).
    install_log_redaction()
    # Per-answer timing JSON lines actually reach stdout (T11.3.1; see install_timing_log).
    timing.install_timing_log()
    settings = get_settings()
    # T12.2.4 (E6, F7): interactive API docs only with ENV=dev -- `None` makes FastAPI skip
    # registering the route entirely (a 404, not an auth-gated one) with ENV=prod.
    docs_enabled = settings.env == "dev"
    app = FastAPI(
        title="SecRAG API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    _add_security_headers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        allow_credentials=False,
    )
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(csp_report.router)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    # Authenticated like /chat/stream: every answer costs an LLM call, so anonymous callers
    # must not reach it. Auth runs before the rate limit so they don't consume a bucket.
    @app.post(
        "/chat",
        response_model=ChatResponse,
        dependencies=[
            Depends(get_current_user),
            Depends(rate_limit_chat),
            Depends(require_csrf),
        ],
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
