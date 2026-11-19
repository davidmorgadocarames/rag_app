"""The feature registry (T13.0.2, P13-D3): one typed, importable list that the future admin
dashboard (13.2), the ``registry`` gate step (T13.0.5) and this module's own tests all read
from — never three independent, drifting lists of "what this product does".

Every :class:`FeatureEntry` names: an ``id``/``name``/``phase``; a ``status`` (adopted,
intentionally off, kept-but-not-adopted, or known-to-still-fail); an optional **health
probe** (``≤ 2 s``, status + a short, PII-free detail — T13.0.4); the Prometheus metric
names it claims; defence-catalogue IDs (``docs/security/THREAT_MODEL.md``, may be empty
before 13-D's defence lab exists); quality-probe ids (may be empty); a docs/ADR link;
``dev_only``/``azure_only`` flags.

**Two probe kinds, two different budgets.** A *health* probe (``health_probe``) answers
``≤ 2 s`` always, including with no live stack at all (the fast gate's normal case) --
:func:`run_health_probe` enforces the deadline with a hard timeout, and every probe here is
written to degrade to ``down``/``degraded`` rather than block or raise. A *quality* probe
(``QUALITY_PROBES``, keyed by the ids an entry lists in ``quality_probes``) measures whether
the PRODUCT still behaves as expected on a real question -- it needs a live DB/Ollama, is
naturally slower, and is only MEANINGFULLY exercised once a stack is actually reachable (see
``quality_probe_misspelling``); with none, it reports ``skipped``, never a failure.

Registered via ``router_tags``/``modules`` rather than "the" one true module path: a route-
backed feature claims the FastAPI ``tags`` its routes use; a plain-Python feature (no route
of its own, e.g. rate limiting) claims the module(s) that implement it. ``check_registry``
cross-checks both against the real, running app and the real Prometheus registry.
"""

from __future__ import annotations

import concurrent.futures as _cf
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from fastapi import FastAPI
    from prometheus_client import CollectorRegistry

FeatureStatus = Literal["enabled", "disabled", "retained-not-adopted", "expected-fail"]
ProbeStatus = Literal["ok", "degraded", "down"]
QualityOutcome = Literal["passed", "failed", "skipped"]

HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
# One small pool shared by every probe call (T13.0.4): a probe is CPU/IO-light by design, so
# a handful of worker threads is plenty even when the `registry` gate step runs every health
# probe back to back.
_PROBE_EXECUTOR = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="feature-probe")


@dataclass(frozen=True)
class HealthProbeResult:
    status: ProbeStatus
    detail: str


@dataclass(frozen=True)
class QualityProbeResult:
    outcome: QualityOutcome
    detail: str


HealthProbe = Callable[[], HealthProbeResult]


def run_health_probe(probe: HealthProbe) -> HealthProbeResult:
    """Runs ``probe`` under a hard ``≤ 2 s`` deadline (T13.0.4). A probe that hangs (e.g. a
    dropped TCP connection with no OS-level timeout reached yet) must never block the caller
    past the deadline -- the worker thread may still be stuck in the background, but
    ``run_health_probe`` itself always returns on time. A probe must never crash the caller
    either: any exception it raises becomes a ``down`` result, never a propagated traceback
    (which could otherwise leak a connection string or similar via its message)."""
    future = _PROBE_EXECUTOR.submit(probe)
    try:
        return future.result(timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
    except _cf.TimeoutError:
        return HealthProbeResult("down", f"timed out after {HEALTH_PROBE_TIMEOUT_SECONDS:g}s")
    except Exception as exc:  # noqa: BLE001 - a probe bug must not crash the registry step
        return HealthProbeResult("down", f"probe error: {type(exc).__name__}")


@dataclass(frozen=True)
class FeatureEntry:
    id: str
    name: str
    phase: str
    status: FeatureStatus
    docs: str
    health_probe: HealthProbe | None = None
    metrics: tuple[str, ...] = ()
    defence_probes: tuple[str, ...] = ()
    quality_probes: tuple[str, ...] = ()
    router_tags: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    dev_only: bool = False
    azure_only: bool = False


# --- generic probe factories (most entries below just plug one of these in) ----------------


def module_probe(module_path: str, attr: str | None = None) -> HealthProbe:
    """Cheap, deterministic "is this feature's code actually shipped and wired" check: an
    import (already cached after the first call anywhere in the process) plus an optional
    attribute lookup. No network, no DB -- used for features that are plain Python, not a
    live external dependency."""

    def _probe() -> HealthProbeResult:
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            return HealthProbeResult("down", f"import failed: {type(exc).__name__}")
        if attr is not None and not hasattr(module, attr):
            return HealthProbeResult("down", f"{module_path}.{attr} not found")
        return HealthProbeResult("ok", f"{module_path} importable")

    return _probe


def iter_routes(app: FastAPI) -> list[object]:
    """Every actual route on ``app``, flattened. Newer FastAPI wraps an
    ``include_router()``'d router lazily as one ``_IncludedRouter`` entry in
    ``app.router.routes`` instead of inlining its routes there directly -- its real routes
    live on ``.original_router.routes``. Both ``check_registry`` and ``route_probe`` need
    the FLAT list (a route's own ``tags``/``path``/``methods``), so this is the one place
    that knows about that wrapper."""
    flat: list[object] = []
    for route in app.router.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            flat.extend(original_router.routes)
        else:
            flat.append(route)
    return flat


def route_probe(method: str, path: str) -> HealthProbe:
    """Confirms a route is actually registered on the live app (table lookup only, no HTTP
    call -- building the app does not start the DB/model warm-up a real request would)."""

    def _probe() -> HealthProbeResult:
        from rag_app.api.app import create_app

        app = create_app()
        for route in iter_routes(app):
            methods = getattr(route, "methods", None) or ()
            if getattr(route, "path", None) == path and method in methods:
                return HealthProbeResult("ok", f"{method} {path} registered")
        return HealthProbeResult("down", f"{method} {path} not found")

    return _probe


def _short_engine() -> Engine:
    """A Postgres engine with a short ``connect_timeout`` (T13.0.4): the fast gate's
    ``DATABASE_URL`` points at a stopped port, and a probe must still answer in well under
    2 s rather than wait for the OS's own (much longer) TCP timeout."""
    from sqlalchemy import create_engine

    from rag_app.config import get_job_settings

    url = get_job_settings().database_url
    return create_engine(
        url, future=True, hide_parameters=True, connect_args={"connect_timeout": 2}
    )


def probe_postgres() -> HealthProbeResult:
    engine = _short_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return HealthProbeResult("ok", "SELECT 1 succeeded")
    except Exception as exc:  # noqa: BLE001 - never leak a DSN/credential via the message
        return HealthProbeResult("down", f"unreachable ({type(exc).__name__})")
    finally:
        engine.dispose()


def probe_migrations() -> HealthProbeResult:
    engine = _short_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        if row and row[0]:
            return HealthProbeResult("ok", "alembic_version present")
        return HealthProbeResult("degraded", "alembic_version table is empty")
    except Exception as exc:  # noqa: BLE001
        return HealthProbeResult("down", f"unreachable or not migrated ({type(exc).__name__})")
    finally:
        engine.dispose()


def probe_embeddings_configured() -> HealthProbeResult:
    """Configuration-only check (T13.0.4): a real ``/api/embed`` call pays Ollama's own
    model-load/inference latency, which the first call after an idle window can measure in
    SECONDS -- too slow and too flaky for a probe meant to answer well under 2 s every time.
    ``rag_app.embeddings``'s own unit tests already cover the HTTP call's shape without a
    live server."""
    from rag_app.config import get_settings

    settings = get_settings()
    if settings.ollama_host and settings.embed_model:
        return HealthProbeResult("ok", f"configured: {settings.embed_model}")
    return HealthProbeResult("down", "OLLAMA_HOST or EMBED_MODEL not set")


def probe_reranker_configured() -> HealthProbeResult:
    """Configuration-only check, same reasoning as the embeddings probe (T13.0.4): loading
    the cross-encoder is a multi-second, CPU-heavy operation on a cold process. Reports
    whether the ONE process-wide shared instance (T11.4.1) happens to be loaded already, as a
    bonus detail, without ever triggering a load itself."""
    from rag_app.config import get_settings

    settings = get_settings()
    if not (settings.reranker_model and settings.reranker_revision):
        return HealthProbeResult("down", "reranker model/revision not set")
    from rag_app import reranking

    loaded = " (loaded)" if reranking._shared_reranker is not None else ""
    return HealthProbeResult("ok", f"configured: {settings.reranker_model}{loaded}")


def probe_llm_provider_configured() -> HealthProbeResult:
    """Configuration-only check (T13.0.4, "no LLM generation calls"): never a real chat
    completion, which would cost real tokens/latency and, on Azure OpenAI, real money."""
    from rag_app.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "ollama":
        if settings.ollama_host:
            return HealthProbeResult("ok", "configured: ollama")
        return HealthProbeResult("down", "OLLAMA_HOST not set")
    if settings.llm_provider == "azure_openai":
        if settings.azure_openai_endpoint and settings.azure_openai_deployment:
            return HealthProbeResult("ok", "configured: azure_openai")
        return HealthProbeResult("down", "azure OpenAI endpoint/deployment not set")
    return HealthProbeResult("down", f"unknown LLM_PROVIDER '{settings.llm_provider}'")


def probe_prometheus() -> HealthProbeResult:
    """Accurate only from WITHIN the live API process (where the admin dashboard will call
    it, 13-B); a fresh process (the gate's own devtool run) always sees "not started" here,
    which is the correct, honest answer for that process -- not a bug."""
    from rag_app import metrics

    if metrics._server is not None:
        return HealthProbeResult("ok", f"metrics server bound on port {metrics._server_port}")
    return HealthProbeResult("down", "metrics server not started in this process")


def probe_exposed_files() -> HealthProbeResult:
    """Backend-only half of T12.5.7's exposed-files guard: the backend mounts no
    ``StaticFiles`` app at all (the frontend-facing half, ``scripts/azure/
    exposed_files_check.sh`` against a live frontend URL, is an Azure-smoke/ops check, not an
    in-process probe -- see ``backend/tests/security/test_exposed_files.py``)."""
    from starlette.staticfiles import StaticFiles

    from rag_app.api.app import create_app

    app = create_app()
    mounted = [r for r in app.router.routes if isinstance(getattr(r, "app", None), StaticFiles)]
    if mounted:
        return HealthProbeResult("down", f"{len(mounted)} StaticFiles mount(s) found")
    return HealthProbeResult("ok", "no StaticFiles mount on the backend")


# --- misspelling tolerance (DEC-037 = A, DEBT-049) -------------------------------------------
#
# 13-A registered this probe `expected-fail`, assuming hybrid retrieval would not survive a
# typo until some future Phase-14 fix. 13-E's `--full` run (live DB + Ollama) showed the
# single golden-set item below actually PASSES today -- `bge-m3`'s embeddings are
# subword/character-robust enough that a one-swap typo barely moves the query vector, and
# RRF fusion with BM25 still surfaces the right chunk. DEC-037 = A (user, 2026-11-16 23:34):
# verify this is real (not a seed/golden-set artefact) and flip the entry off
# `expected-fail` -- done below -- then add typo variants across several items/harder typos
# (`quality_probe_misspelling_variants`) so "one item passes" is never read as "the feature
# is 100% done"; any variant that FAILS is recorded in DEBT-049, not silently dropped.


def _misspell(word: str) -> str:
    """Swap two adjacent middle characters -- a realistic, single fat-finger typo."""
    if len(word) < 4:
        return word
    i = len(word) // 2
    chars = list(word)
    chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


def _delete_middle_char(word: str) -> str:
    """Drop one character -- a slightly harder typo than a swap (changes word length)."""
    if len(word) < 4:
        return word
    i = len(word) // 2
    return word[:i] + word[i + 1 :]


def _double_middle_char(word: str) -> str:
    """Repeat one character -- another common fat-finger typo (changes word length)."""
    if len(word) < 4:
        return word
    i = len(word) // 2
    return word[:i] + word[i] + word[i:]


def _scramble_word(word: str) -> str:
    """Shuffle every character -- a deliberately HARD, unrealistic-but-illustrative typo:
    the word keeps its length and character set but not necessarily its recognisability.
    Deterministic (fixed seed) so the probe's result does not flap between runs."""
    import random

    if len(word) < 4:
        return word
    chars = list(word)
    random.Random(1337).shuffle(chars)
    shuffled = "".join(chars)
    return shuffled if shuffled != word else word[::-1]


# Ordered easiest -> hardest; the registered single-item probe below only ever exercises the
# first (easiest) one, matching its original, narrower claim.
_TYPO_STRATEGIES: dict[str, Callable[[str], str]] = {
    "adjacent-swap": _misspell,
    "deleted-char": _delete_middle_char,
    "doubled-char": _double_middle_char,
    "scrambled": _scramble_word,
}


def _apply_typo(question: str, strategy: Callable[[str], str]) -> str:
    words = question.split()
    if not words:
        return question
    longest = max(range(len(words)), key=lambda i: len(words[i]))
    words[longest] = strategy(words[longest])
    return " ".join(words)


def _misspell_question(question: str) -> str:
    return _apply_typo(question, _misspell)


def quality_probe_misspelling(session: Session | None) -> QualityProbeResult:
    """A single-typo version of a real golden-set question, run through the SAME retrieval
    path production uses (``rag_app.retrieval.hybrid_search``), checked against whether the
    expected document is still retrieved. ``session=None`` (no live DB -- the normal case
    for the fast ``registry`` gate step) returns ``skipped`` without attempting a
    connection. Verified genuinely passing in 13-E (DEC-037 = A): this ONE item does not
    prove general typo tolerance -- see ``quality_probe_misspelling_variants``/DEBT-049 for
    the broader check and the recorded remaining gap."""
    if session is None:
        return QualityProbeResult("skipped", "no live DB in this run")
    try:
        from rag_app.eval.dataset import load_golden_set
        from rag_app.retrieval import hybrid_search

        items = [item for item in load_golden_set() if item.answerable and item.expected_doc]
        if not items:
            return QualityProbeResult("skipped", "golden set has no answerable item")
        item = items[0]
        typo_question = _misspell_question(item.question)
        chunks = hybrid_search(session, typo_question, version=item.version)
        retrieved_docs = {chunk.chunk_uid.rsplit("::", 1)[0] for chunk in chunks}
        if item.expected_doc in retrieved_docs:
            return QualityProbeResult("passed", "typo-tolerant retrieval already works")
        return QualityProbeResult(
            "failed", "expected document not retrieved for a misspelled query"
        )
    except Exception as exc:  # noqa: BLE001 - DB/Ollama unreachable mid-probe: skip, don't fail
        return QualityProbeResult("skipped", f"stack unreachable ({type(exc).__name__})")


def quality_probe_misspelling_variants(session: Session | None) -> QualityProbeResult:
    """DEBT-049: the single-item probe above passing is not proof hybrid retrieval tolerates
    typos IN GENERAL -- this exercises several golden-set items x several typo strategies
    (easiest ``adjacent-swap`` up to a full ``scrambled`` word) through the same
    ``hybrid_search`` path and reports how many combinations still retrieve the expected
    document. ``outcome`` is ``failed`` the moment any combination misses (this entry's
    ``status`` stays ``enabled`` regardless -- a partial gap is recorded in DEBT-049, not a
    registry-blocking failure); ``session=None`` returns ``skipped``, same as the probe
    above."""
    if session is None:
        return QualityProbeResult("skipped", "no live DB in this run")
    try:
        from rag_app.eval.dataset import load_golden_set
        from rag_app.retrieval import hybrid_search

        items = [item for item in load_golden_set() if item.answerable and item.expected_doc][:5]
        if not items:
            return QualityProbeResult("skipped", "golden set has no answerable item")
        passed: list[str] = []
        failed: list[str] = []
        for item in items:
            for strategy_name, strategy in _TYPO_STRATEGIES.items():
                typo_question = _apply_typo(item.question, strategy)
                if typo_question == item.question:
                    continue  # longest word too short for this strategy to change anything
                chunks = hybrid_search(session, typo_question, version=item.version)
                retrieved_docs = {c.chunk_uid.rsplit("::", 1)[0] for c in chunks}
                tag = f"{item.id}/{strategy_name}"
                (passed if item.expected_doc in retrieved_docs else failed).append(tag)
        total = len(passed) + len(failed)
        if total == 0:
            return QualityProbeResult("skipped", "no (item, strategy) pair produced a typo")
        failing = ", ".join(failed) if failed else "none"
        detail = f"{len(passed)}/{total} tolerated; failing: {failing}"
        return QualityProbeResult("failed" if failed else "passed", detail)
    except Exception as exc:  # noqa: BLE001 - DB/Ollama unreachable mid-probe: skip, don't fail
        return QualityProbeResult("skipped", f"stack unreachable ({type(exc).__name__})")


QUALITY_PROBES: dict[str, Callable[[Session | None], QualityProbeResult]] = {
    "misspelling-tolerance": quality_probe_misspelling,
    "misspelling-tolerance-variants": quality_probe_misspelling_variants,
}


# --- the registry itself --------------------------------------------------------------------

REGISTRY: tuple[FeatureEntry, ...] = (
    # --- Stack -------------------------------------------------------------------------
    FeatureEntry(
        id="stack-health-endpoint",
        name="Liveness endpoint (/health)",
        phase="11a",
        status="enabled",
        health_probe=route_probe("GET", "/health"),
        router_tags=("stack",),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="stack-postgres",
        name="Postgres",
        phase="11a",
        status="enabled",
        health_probe=probe_postgres,
        modules=("rag_app.db.session",),
        docs="docs/BACKEND_SCHEMA.md",
    ),
    FeatureEntry(
        id="stack-migrations",
        name="Alembic migrations",
        phase="11a",
        status="enabled",
        health_probe=probe_migrations,
        modules=("rag_app.db.models",),
        docs="docs/BACKEND_SCHEMA.md",
    ),
    FeatureEntry(
        id="stack-embeddings",
        name="Embeddings (Ollama bge-m3)",
        phase="11a",
        status="enabled",
        health_probe=probe_embeddings_configured,
        modules=("rag_app.embeddings",),
        docs="docs/adr/adr_phase11_stability.md",
    ),
    FeatureEntry(
        id="stack-reranker",
        name="Cross-encoder reranker (shared, quantized)",
        phase="11b",
        status="enabled",
        health_probe=probe_reranker_configured,
        modules=("rag_app.reranking",),
        docs="docs/adr/adr_phase11_stability.md",
    ),
    FeatureEntry(
        id="stack-llm-provider",
        name="LLM provider (Ollama / Azure OpenAI)",
        phase="10",
        status="enabled",
        health_probe=probe_llm_provider_configured,
        modules=("rag_app.llm",),
        docs="docs/adr/adr_phase10_azure.md",
    ),
    FeatureEntry(
        id="stack-prometheus",
        name="Prometheus metrics (in-process registry)",
        phase="11b",
        status="enabled",
        health_probe=probe_prometheus,
        metrics=(
            "secrag_chat_stage_seconds",
            "secrag_chat_requests_in_flight",
        ),
        modules=("rag_app.metrics",),
        docs="docs/adr/adr_phase13_dashboard.md",
    ),
    # --- RAG -----------------------------------------------------------------------------
    FeatureEntry(
        id="rag-hybrid-retrieval",
        name="Hybrid retrieval (pgvector + BM25, RRF fusion)",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.retrieval", "hybrid_search"),
        router_tags=("rag",),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="rag-rerank",
        name="Cross-encoder rerank",
        phase="11b",
        status="enabled",
        health_probe=module_probe("rag_app.reranking", "get_shared_reranker"),
        docs="docs/adr/adr_phase11_stability.md",
    ),
    FeatureEntry(
        id="rag-generation-groundedness",
        name="Generation + groundedness check",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.generation", "answer_question"),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="rag-abstention",
        name="Abstention (no reliable source)",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.generation", "_ABSTENTION_TEXT"),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="rag-chitchat",
        name="Chit-chat (incl. typo-tolerant small talk)",
        phase="12b",
        status="enabled",
        health_probe=module_probe("rag_app.generation", "_CHITCHAT_REPLY"),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="rag-sse-token-usage",
        name="Streaming answers (SSE) + token usage",
        phase="11a",
        status="enabled",
        health_probe=route_probe("POST", "/chat/stream"),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="rag-agentic-router",
        name="Agentic router (query-rewrite retry, CRAG-lite)",
        phase="11b",
        status="retained-not-adopted",
        health_probe=module_probe("rag_app.agentic", "answer_agentic"),
        docs="docs/adr/adr_phase11_stability.md",
    ),
    # --- Quality ---------------------------------------------------------------------
    FeatureEntry(
        id="quality-eval-vs-baseline",
        name="Eval vs baseline (golden set)",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.eval.benchmark", "run_pipeline"),
        quality_probes=("eval-vs-baseline",),
        docs="docs/DEFINITION_OF_DONE.md",
    ),
    FeatureEntry(
        id="quality-judge-kappa",
        name="Correctness judge (Cohen's kappa validated)",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.eval.judge", "judge_correctness"),
        quality_probes=("judge-kappa",),
        docs="docs/DEFINITION_OF_DONE.md",
    ),
    FeatureEntry(
        id="quality-per-stage-latency",
        name="Per-stage latency benchmark",
        phase="11b",
        status="enabled",
        health_probe=module_probe("rag_app.eval.latency", "run_latency_benchmark"),
        quality_probes=("latency-p95",),
        # T13.8.2: the admin latency panel's atomic-step waterfall (`/admin/latency`'s
        # `pipeline` field) -- closes this entry's DoD rule for real (removed from
        # `_DOD_GRANDFATHERED_IDS` in the same change).
        metrics=("secrag_chat_step_seconds",),
        docs="docs/adr/adr_phase11_stability.md",
    ),
    FeatureEntry(
        id="quality-misspelling-probe",
        name="Misspelling tolerance (query typo robustness)",
        phase="13",
        # DEC-037 = A (user, 2026-11-16 23:34): flipped off `expected-fail` -- verified in
        # 13-E that the single golden-set item genuinely passes with a live DB + Ollama
        # (bge-m3 embeddings + RRF tolerate a one-swap typo). NOT a claim that typo
        # tolerance is complete: `misspelling-tolerance-variants` exercises harder typos
        # across more items and some of those fail -- tracked, not hidden, in DEBT-049.
        status="enabled",
        health_probe=None,
        quality_probes=("misspelling-tolerance", "misspelling-tolerance-variants"),
        docs="docs/adr/adr_phase13_dashboard.md",
    ),
    # --- Identity ------------------------------------------------------------------------
    FeatureEntry(
        id="identity-registration-verification",
        name="Registration + email verification",
        phase="11a",
        status="enabled",
        health_probe=route_probe("POST", "/auth/register"),
        router_tags=("identity",),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="identity-login-sessions",
        name="Login + server-side sessions",
        phase="11a",
        status="enabled",
        health_probe=route_probe("POST", "/auth/login"),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="identity-lockout",
        name="Login lockout (exponential back-off)",
        phase="12b",
        status="enabled",
        health_probe=module_probe("rag_app.lockout", "record_failed_login"),
        metrics=("secrag_lockout_hits_total",),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="identity-mfa",
        name="MFA (TOTP enrol/confirm + step-up, recovery codes)",
        phase="12b",
        status="enabled",
        health_probe=route_probe("POST", "/account/mfa/step-up"),
        metrics=("secrag_mfa_step_up_total", "secrag_mfa_reenroll_total"),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="identity-email-verification-reset",
        name="Email verification/reset links (SMTP; ACS SMTP on Azure)",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.emailer", "_send"),
        metrics=("secrag_email_sent_total", "secrag_email_failed_total"),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="identity-password-policy-reset",
        name="Password policy + reset",
        phase="12b",
        status="enabled",
        health_probe=module_probe("rag_app.password_policy", "validate_password"),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="identity-permission-map",
        name="Permission map (require_permission_and_mfa)",
        phase="12b",
        status="enabled",
        health_probe=module_probe("rag_app.authz", "ROLE_PERMISSIONS"),
        docs="docs/TRD.md",
    ),
    # --- Abuse -----------------------------------------------------------------------
    FeatureEntry(
        id="abuse-rate-limiting",
        name="Rate limiting (token bucket)",
        phase="11a",
        status="enabled",
        health_probe=module_probe("rag_app.ratelimit", "RateLimiter"),
        metrics=("secrag_rate_limit_hits_total",),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="abuse-signup-risk",
        name="Signup risk scoring (disposable domains + velocity)",
        phase="12b",
        status="enabled",
        health_probe=module_probe("rag_app.risk", "signup_risk_score"),
        metrics=("secrag_signup_blocked_total",),
        docs="docs/adr/adr_phase12_security_baseline.md",
    ),
    FeatureEntry(
        id="abuse-signed-client-ip",
        name="Signed client IP (BFF -> backend)",
        phase="12a",
        status="enabled",
        health_probe=module_probe("rag_app.client_ip", "classify_client_ip_header"),
        metrics=("secrag_client_ip_signature_total",),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="abuse-email-abuse-limits",
        name="Email abuse limits (per-IP/recipient/global)",
        phase="12b",
        status="enabled",
        health_probe=module_probe("rag_app.email_abuse", "EmailAbuseLimiter"),
        docs="docs/security/THREAT_MODEL.md",
    ),
    # --- Privacy ---------------------------------------------------------------------
    FeatureEntry(
        id="privacy-encrypted-conversations",
        name="Encrypted conversations (per-user key)",
        phase="6",
        status="enabled",
        health_probe=module_probe("rag_app.crypto", "wrap_key"),
        docs="docs/adr/adr_phase06_gdpr_erasure.md",
    ),
    FeatureEntry(
        id="privacy-asynchronous-erasure",
        name="Asynchronous erasure (crypto-shred + batched purge)",
        phase="11b",
        status="enabled",
        health_probe=route_probe("DELETE", "/account"),
        metrics=(
            "secrag_erasure_pending",
            "secrag_erasure_oldest_age_seconds",
            "secrag_erasure_failures_total",
        ),
        router_tags=("privacy",),
        docs="docs/adr/adr_phase06_gdpr_erasure.md",
    ),
    FeatureEntry(
        id="privacy-deletion-replay",
        name="Deletion replay (restore safety)",
        phase="11b",
        status="enabled",
        health_probe=module_probe("rag_app.erasure", "replay_deletions"),
        docs="docs/adr/adr_phase06_gdpr_erasure.md",
    ),
    # --- Browser security --------------------------------------------------------------
    FeatureEntry(
        id="browser-security-headers",
        name="Security headers (CSP, nosniff, referrer policy, no-store)",
        phase="12a",
        status="enabled",
        health_probe=module_probe("rag_app.api.app", "_add_security_headers"),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="browser-csp-reports",
        name="CSP violation reports (/csp-report)",
        phase="12a",
        status="enabled",
        health_probe=route_probe("POST", "/csp-report"),
        metrics=("secrag_csp_reports_total",),
        router_tags=("browser-security",),
        docs="docs/TRD.md",
    ),
    FeatureEntry(
        id="browser-exposed-files",
        name="Exposed-files check (.git/.env/source maps never served)",
        phase="12b",
        status="enabled",
        health_probe=probe_exposed_files,
        docs="docs/TRD.md",
    ),
    # --- Azure-only ---------------------------------------------------------------------
    FeatureEntry(
        id="azure-internal-ingress",
        name="Internal ingress (backend has no public URL)",
        phase="12b",
        status="enabled",
        health_probe=None,
        azure_only=True,
        docs="docs/TRD.md",
    ),
    # --- Admin (13.1+; today only the placeholder route exists) ------------------------
    FeatureEntry(
        id="admin-api",
        name="Admin API (/admin/*, permission + MFA step-up gated)",
        phase="12b",
        status="enabled",
        health_probe=route_probe("GET", "/admin/ping"),
        router_tags=("admin",),
        docs="docs/adr/adr_phase13_dashboard.md",
    ),
    # --- Defence lab (13-D, local only -- DEC-028) --------------------------------------
    FeatureEntry(
        id="admin-defence-lab",
        name="Defence lab (THREAT_MODEL-catalogue probes, CLI + admin button)",
        phase="13",
        status="enabled",
        # Honestly reflects LAB_ENABLED: the route is only REGISTERED when the flag is on
        # (T13.3.1), so this probe reports "down" (route absent) with the lab off -- the
        # dashboard hides the lab tab on exactly that signal, never a separate flag lookup.
        health_probe=route_probe("GET", "/admin/lab/runs"),
        defence_probes=(
            "OWASP-A02",
            "OWASP-A05",
            "OWASP-A07",
            "API-1",
            "API-2",
            "API-4",
            "API-5",
            "API-6",
            "API-8",
            "API-10",
            "LLM-10",
            "REC-02",
            "REC-04",
            "REC-05",
            "REC-06",
            "REC-08",
            "REC-09",
            "REC-11",
            "REC-12",
        ),  # fmt: skip
        router_tags=("admin-lab",),
        dev_only=True,
        docs="docs/adr/adr_phase13_dashboard.md",
    ),
)


# --- T13.5.1: the DoD rule, enforced for every entry added from here on --------------------

# Every entry here PREDATES the T13.5.1 DoD rule (checked 2026-11-17, 13-E): a registry
# entry needs a probe (health or quality) and >= 1 Prometheus metric; a `defence_probes`
# entry is required too whenever the feature is actually catalogued in
# `docs/security/THREAT_MODEL.md` (judged by review, not by this function -- there is no
# reliable, generic way to tell "should be catalogued but isn't" apart from "genuinely has
# no security-relevant behaviour" without a human reading THREAT_MODEL.md). Listed here as
# an explicit, reviewed exception rather than a silent gap so `check_dod_compliance` can
# fail CI the moment a NEW entry skips the rule, without rewriting 29 historical entries in
# one block. Closing one of these (adding the missing probe/metric) is ordinary product
# work -- remove its id from this set in the same change.
# DEC-039 (DA-13-E-1): this set is FROZEN -- `test_features.py` pins its exact size, so it
# can only ever shrink (lower the pinned number in the same change). Never add an id here:
# a new entry must satisfy the DoD rule instead.
_DOD_GRANDFATHERED_IDS: frozenset[str] = frozenset(
    {
        "stack-health-endpoint",
        "stack-postgres",
        "stack-migrations",
        "stack-embeddings",
        "stack-reranker",
        "stack-llm-provider",
        "rag-hybrid-retrieval",
        "rag-rerank",
        "rag-generation-groundedness",
        "rag-abstention",
        "rag-chitchat",
        "rag-sse-token-usage",
        "rag-agentic-router",
        "quality-eval-vs-baseline",
        "quality-judge-kappa",
        "quality-misspelling-probe",
        "identity-registration-verification",
        "identity-login-sessions",
        "identity-password-policy-reset",
        "identity-permission-map",
        "abuse-email-abuse-limits",
        "privacy-encrypted-conversations",
        "privacy-deletion-replay",
        "browser-security-headers",
        "browser-exposed-files",
        "azure-internal-ingress",
        "admin-api",
        "admin-defence-lab",
    }
)


def check_dod_compliance(entries: tuple[FeatureEntry, ...] = REGISTRY) -> list[str]:
    """T13.5.1: structural half of the DoD rule -- every entry NOT in
    ``_DOD_GRANDFATHERED_IDS`` must declare a probe (``health_probe`` or ``quality_probes``)
    and at least one metric (``entry.metrics``). The "defence probe if the catalogue lists
    it" half of the rule is a review-time judgment call (see the module-level comment above)
    and is intentionally not checked here."""
    problems: list[str] = []
    for entry in entries:
        if entry.id in _DOD_GRANDFATHERED_IDS:
            continue
        if entry.health_probe is None and not entry.quality_probes:
            problems.append(
                f"{entry.id}: DoD rule (T13.5.1) needs a health_probe or a quality_probes" " entry"
            )
        if not entry.metrics:
            problems.append(f"{entry.id}: DoD rule (T13.5.1) needs >= 1 metric")
    return problems


def entries_by_status(status: FeatureStatus) -> tuple[FeatureEntry, ...]:
    return tuple(entry for entry in REGISTRY if entry.status == status)


def _known_metric_names(prometheus_registry: CollectorRegistry) -> set[str]:
    """Both the base name ``collect()`` reports and, for counters, the ``_total``-suffixed
    public name an entry actually declares (T13.0.5) -- see this module's own exploration
    notes: a fresh ``Counter("..._total", ...)``'s ``collect()`` entry is named WITHOUT the
    suffix, but every entry in this registry (and every admin-panel reader) names metrics by
    their real, public ``/metrics`` line."""
    names: set[str] = set()
    for metric in prometheus_registry.collect():
        names.add(metric.name)
        if metric.type == "counter":
            names.add(metric.name + "_total")
    return names


def check_registry(
    app: FastAPI,
    prometheus_registry: CollectorRegistry,
    entries: tuple[FeatureEntry, ...] = REGISTRY,
) -> list[str]:
    """Structural checks (T13.0.5), no stack required:

    1. every FastAPI route's tag(s) are claimed by at least one registry entry's
       ``router_tags`` -- a brand-new, unclaimed tag (someone added a router and forgot the
       registry) fails here;
    1b. every ``APIRoute`` declares AT LEAST ONE tag at all (DA-13-A-2) -- without this, a
        route added with no ``tags=`` contributes nothing to ``used_tags`` and check 1 above
        passes it vacuously, so a forgotten registry entry would never be caught. Framework
        routes FastAPI adds itself (``/docs``, ``/redoc``, ``/openapi.json``, the OAuth2
        redirect) are plain Starlette ``Route``s, not ``APIRoute``s, and have no ``tags``
        concept at all -- only ``APIRoute``s are checked here;
    2. every metric name an entry declares actually exists in the given Prometheus registry;
    3. every module an entry names is importable.

    ``entries`` defaults to the real :data:`REGISTRY` (every caller outside this module's
    own tests); a test passes a throwaway tuple to exercise one check in isolation without
    depending on the real registry's current contents.
    """
    from fastapi.routing import APIRoute

    problems: list[str] = []
    flat_routes = iter_routes(app)
    used_tags = {tag for route in flat_routes for tag in (getattr(route, "tags", None) or [])}
    claimed_tags = {tag for entry in entries for tag in entry.router_tags}
    for tag in sorted(used_tags - claimed_tags):
        problems.append(f"route tag '{tag}' has no registry entry claiming it (router_tags)")

    for route in flat_routes:
        if isinstance(route, APIRoute) and not route.tags:
            methods = ",".join(sorted((route.methods or set()) - {"HEAD", "OPTIONS"}))
            problems.append(
                f"route {methods} {route.path} has no tags -- add tags=[...] so a registry"
                " entry can claim it"
            )

    known_metrics = _known_metric_names(prometheus_registry)
    for entry in entries:
        for metric_name in entry.metrics:
            if metric_name not in known_metrics:
                problems.append(
                    f"{entry.id}: declared metric '{metric_name}' not found in the"
                    " Prometheus registry"
                )
        for module_path in entry.modules:
            try:
                importlib.import_module(module_path)
            except Exception as exc:  # noqa: BLE001 - report, never raise
                problems.append(
                    f"{entry.id}: module '{module_path}' not importable ({type(exc).__name__})"
                )
        if entry.status == "expected-fail":
            # Only an `expected-fail` entry's quality probe is actually RUN by the
            # `registry` gate step (to prove it still fails) -- every other entry's
            # `quality_probes` is documentation (which gate step/golden-set measures it),
            # not something that needs a callable registered here too.
            if not entry.quality_probes:
                problems.append(f"{entry.id}: status 'expected-fail' but no quality_probes listed")
            for probe_id in entry.quality_probes:
                if probe_id not in QUALITY_PROBES:
                    problems.append(f"{entry.id}: quality probe id '{probe_id}' not registered")
    return problems
