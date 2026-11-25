"""FastAPI application factory and routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import text

from rag_app import __version__, authz, csp_report, metrics, reranking, timing, wake, warmup
from rag_app.api import admin, auth, conversations
from rag_app.api.deps import (
    AnswerFn,
    ReserveFn,
    SessionDep,
    get_answer_reserver,
    get_answerer,
    rate_limit_chat,
)
from rag_app.api.schemas import ChatRequest, ChatResponse, CitationOut, HealthResponse
from rag_app.authz import require_permission
from rag_app.config import Settings, check_dev_only_flags, get_settings, validate_api_settings
from rag_app.csrf import require_csrf
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
    this API port — only once the fail-closed checks above have passed. Then loads and
    warms up the ONE shared cross-encoder reranker (T11.4.1): the first real chat request
    pays no model-load/first-inference cost, because it is already resident by the time
    this coroutine yields and the app starts serving traffic.

    T13.13.1 (DEBT-061): fires a background warm-up embed to Ollama
    (``rag_app.warmup.trigger_warm_up``) BEFORE the (synchronous, blocking) reranker
    warm-up above -- order matters here, measured in the ADR (13-J4): Ollama is a
    SEPARATE Container App, also scaled to zero on Azure, and its own cold start (image
    pull/container start + loading ``bge-m3``) is the slowest single thing in the whole
    pipeline (~19.7s avg). Firing the background thread first lets Ollama's cold start run
    CONCURRENTLY with the full remainder of this backend's own start-up (reranker
    load/warm-up, everything else below) instead of only overlapping whatever tiny gap is
    left between "this backend is ready" and "a request's classify step reaches
    ``embed_query()``" -- the fix is a few lines, reordering costs nothing, and the
    overlap window this buys is exactly the reranker warm-up's own duration. This call
    returns immediately either way (it only starts a background thread): readiness/
    ``/health`` never waits on it, and a failure there is only logged, never raised here.
    """
    settings = get_settings()
    startup_checks(settings)
    metrics.start_metrics_server(settings.metrics_port, addr=settings.metrics_bind_addr)
    warmup.trigger_warm_up()
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
    # T12.5.3: CORS removed entirely (the BFF-based frontend is the only caller, same-origin
    # server-to-server, never a browser preflight) -- the legacy Bearer-only frontend that
    # needed cross-origin access is gone (see `get_current_user` in `rag_app.api.auth`).
    app.include_router(auth.router)
    # T12.5.2 (route walker): `/auth/me` and `DELETE /account` live in `rag_app.api.auth`,
    # which cannot import `rag_app.authz` itself (a module cycle -- `authz.require_permission`
    # needs `auth.CurrentUserDep`; see `authz.declare_permission`'s docstring). Both routes
    # already enforce authentication via `CurrentUserDep`; this only registers the
    # permission for the walker, there is nothing left to decide at request time.
    authz.declare_permission("GET", "/auth/me", "user:base")
    authz.declare_permission("DELETE", "/account", "user:base")
    app.include_router(conversations.router)
    app.include_router(csp_report.router)
    app.include_router(wake.router)
    app.include_router(admin.router)
    # T13.3.1: the defence lab's routes are registered ONLY when LAB_ENABLED is on -- the
    # default is off, and the start-up guard (check_dev_only_flags, called from `lifespan`
    # below) refuses to serve AT ALL with ENV=prod whatever this setting says, so a
    # production/Azure deployment never reaches a request against `/admin/lab/*` either way.
    # Conditional registration (rather than a permission check inside the route) means the
    # routes are genuinely ABSENT with the lab off -- a 404, not a 403.
    if settings.lab_enabled:
        from rag_app.api import lab as lab_routes

        app.include_router(lab_routes.router)

    # T13.0.5: a consistent `tags=` on every route (not just the ones with an explicit
    # security dependency) — the `registry` gate step cross-checks every tag that shows up
    # anywhere in the app against `rag_app.features.REGISTRY`, so a brand-new, untagged (or
    # mistagged) router fails the gate instead of going unnoticed.
    @app.get("/health", response_model=HealthResponse, tags=["stack"])
    def health() -> HealthResponse:
        """Liveness: the process answers HTTP at all -- NEVER touches the DB/Ollama (T16.2.1,
        k8s livenessProbe). A transient dependency outage must never trigger a container
        restart storm; ``/health/ready`` below is the one that checks dependencies, and only
        the k8s readinessProbe (never liveness) acts on it by pulling the pod out of the
        Service. Unauthenticated, no rate limit, same as every other probe here (cheap,
        process-only, meant to be hit every few seconds)."""
        return HealthResponse(status="ok")

    @app.get("/health/ready", response_model=HealthResponse, tags=["stack"])
    def health_ready(session: SessionDep) -> HealthResponse:
        """Readiness: the backend's one real dependency, Postgres, is reachable right now
        (T16.2.1, k8s readinessProbe). A failure here pulls the pod out of the Service
        (no new traffic routed to it) without restarting the process -- the process itself
        may well be fine, only its DB connection is not, and a restart would not fix that."""
        try:
            session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - any DB failure means "not ready", not a 500
            raise HTTPException(status_code=503, detail="database unreachable") from exc
        return HealthResponse(status="ok")

    # Authenticated like /chat/stream: every answer costs an LLM call, so anonymous callers
    # must not reach it. Auth runs before the rate limit so they don't consume a bucket.
    @app.post(
        "/chat",
        response_model=ChatResponse,
        tags=["rag"],
        dependencies=[
            Depends(require_permission("user:base", route=("POST", "/chat"), matrix=False)),
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
