"""Prometheus metrics for the chat pipeline (T11.3.2, TF4).

Served on a **separate internal port** (``Settings.metrics_port``), never the API port:
mixing them would expose route/volume/error shape on a port reachable from the browser.
Only the API's own lifespan starts this server (``start_metrics_server``); the FastAPI
app in ``rag_app.api.app`` never registers a ``/metrics`` route itself.

Histograms carry only the ``stage`` label (a small, fixed vocabulary: classify, embed,
hybrid_search, rerank_load, rerank_inference, generate, groundedness, total, ttft) — never
a user id, IP, question text or answer text (see PHASE_PLANNING appendix "Prometheus in
one page"). The in-flight gauge has no labels at all.

T13.8.1 adds a SECOND histogram, ``secrag_chat_step_seconds``, labelled ``stage``+``step``
with its OWN fixed, reviewed allow-list (``STEP_ALLOWLIST``) of atomic steps within the
UI-facing pipeline stages (classifying/retrieving/reranking/generating/checking/
responding) -- same no-PII discipline, enforced at write time by :func:`observe_step`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from wsgiref.simple_server import WSGIServer

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

REGISTRY = CollectorRegistry()

STAGE_SECONDS = Histogram(
    "secrag_chat_stage_seconds",
    "Duration of one chat-pipeline stage (seconds), labelled only by stage name.",
    ["stage"],
    registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

CHAT_IN_FLIGHT = Gauge(
    "secrag_chat_requests_in_flight",
    "Chat requests (streaming or not) currently being answered.",
    registry=REGISTRY,
)

# T12.1.6 / T12.3.0a (DEBT-038): one ``result`` label, a small fixed vocabulary -- never an
# IP or a path (log hygiene) -- counted only once CLIENT_IP_HMAC_KEYS is actually set:
#   verified  a fresh, correctly-signed header was trusted
#   fallback  no X-SecRAG-Client-IP header was present at all
#   stale     a correctly-signed header outside SIGNATURE_WINDOW_SECONDS (e.g. a cold start)
#   forged    a header present but its signature matches no configured key
CLIENT_IP_SIGNATURE_TOTAL = Counter(
    "secrag_client_ip_signature_total",
    "Requests by signed-client-IP outcome (CLIENT_IP_HMAC_KEYS set).",
    ["result"],
    registry=REGISTRY,
)

# T12.4.7 (rag_app.emailer): one ``kind`` label, a small fixed vocabulary (verification /
# already_registered / password_reset / password_reset_unknown) -- never the recipient
# address (log hygiene). SENT counts a completed SMTP send; FAILED counts a caught
# ``OSError`` (transient SMTP error, e.g. Mailpit not accepting connections yet) or SMTP not
# configured at all -- the SAME event emailer.py already logs without the address/exception
# text (T11.2.15, R6-5).
EMAIL_SENT_TOTAL = Counter(
    "secrag_email_sent_total",
    "Emails successfully handed to the SMTP server, by kind.",
    ["kind"],
    registry=REGISTRY,
)
EMAIL_FAILED_TOTAL = Counter(
    "secrag_email_failed_total",
    "Emails NOT sent (SMTP not configured or a transient SMTP error), by kind.",
    ["kind"],
    registry=REGISTRY,
)

# T13.0.6 (DEC-028 A5, D-ER): the security panel, banner alerts and the registry's "≥ 1
# metric per feature" DoD rule need counters/gauges that did not exist before Phase 13 — only
# the five metrics above did. Every label below is a small, fixed, developer-chosen
# vocabulary -- never a user id, e-mail address or IP (log hygiene, same reasoning as
# CLIENT_IP_SIGNATURE_TOTAL's ``result`` label):
#   route_group  chat / login / email / csp_report / account / mfa / wake (RATE_LIMIT_HITS_TOTAL)
#   reason       disposable_email / signup_velocity (SIGNUP_BLOCKED_TOTAL)
#   result       success / failure (MFA_STEP_UP_TOTAL, MFA_REENROLL_TOTAL)
RATE_LIMIT_HITS_TOTAL = Counter(
    "secrag_rate_limit_hits_total",
    "Requests rejected (429) by a token-bucket rate limiter, by route group.",
    ["route_group"],
    registry=REGISTRY,
)

LOCKOUT_HITS_TOTAL = Counter(
    "secrag_lockout_hits_total",
    "Login attempts rejected (429) because the account is currently locked out (T12.4.2).",
    registry=REGISTRY,
)

SIGNUP_BLOCKED_TOTAL = Counter(
    "secrag_signup_blocked_total",
    "Registrations rejected by the signup risk check (rag_app.risk), by reason.",
    ["reason"],
    registry=REGISTRY,
)

CSP_REPORTS_TOTAL = Counter(
    "secrag_csp_reports_total",
    "CSP violation reports received at /csp-report (one per logged directive/origin entry).",
    registry=REGISTRY,
)

MFA_STEP_UP_TOTAL = Counter(
    "secrag_mfa_step_up_total",
    "MFA step-up attempts (/account/mfa/step-up), by result.",
    ["result"],
    registry=REGISTRY,
)

# DEBT-051/DEC-041: the proof check that guards re-enrolling an ALREADY-ENABLED record
# (/account/mfa/enroll) -- only incremented on that guarded path, never on a plain
# first-time enrolment (which has no proof to check). Same "audit trail" role for this
# route as MFA_STEP_UP_TOTAL already plays for step-up.
MFA_REENROLL_TOTAL = Counter(
    "secrag_mfa_reenroll_total",
    "Re-enrolment attempts on an already-enabled MFA record (/account/mfa/enroll), by result.",
    ["result"],
    registry=REGISTRY,
)

# D-ER (asynchronous erasure): published by rag_app.erasure.refresh_erasure_gauges, called
# right after a request commits and by the purger's own bookkeeping — see that function's
# docstring for the known cross-process visibility limitation (the purger Job is a separate
# process on Azure; ADR phase 13 "registry" section).
ERASURE_PENDING = Gauge(
    "secrag_erasure_pending",
    "Open erasure tombstones (pending/running/failed) as of the last refresh (D-ER).",
    registry=REGISTRY,
)

ERASURE_OLDEST_AGE_SECONDS = Gauge(
    "secrag_erasure_oldest_age_seconds",
    "Age in seconds of the oldest open erasure tombstone, 0 when none is open (D-ER).",
    registry=REGISTRY,
)

ERASURE_FAILURES_TOTAL = Counter(
    "secrag_erasure_failures_total",
    "Erasure tombstones the purger marked 'failed' (D-ER).",
    registry=REGISTRY,
)

# T13.13.1/T13.13.2 (DEBT-061): a background warm-up embed to Ollama, fired from the API
# lifespan (once, at start-up) and from GET /wake (on a cold `/login`/`/chat` page load) --
# see ``rag_app.warmup``. ``result`` is "ok"/"failed"/"skipped" (a skip means one was already
# in flight, the single-flight guard did its job, not an error). Deliberately NOT the same
# channel as the per-answer ``secrag_chat_stage_seconds``/``secrag_chat_step_seconds``
# histograms -- a warm-up is never a real answer, so it must never be averaged into either.
OLLAMA_WARMUP_TOTAL = Counter(
    "secrag_ollama_warmup_total",
    "Background warm-up embed calls to Ollama (T13.13.1/T13.13.2), by result.",
    ["result"],
    registry=REGISTRY,
)

# T13.8.1: the atomic steps WITHIN each UI-facing pipeline stage (``generation.StreamStage``'s
# own vocabulary: classifying/retrieving/reranking/generating/checking/responding -- a
# DIFFERENT, higher-level grouping than STAGE_SECONDS's own ``stage`` label, e.g. "retrieving"
# covers "embed"+"hybrid_search"). A FIXED, reviewed allow-list, in PIPELINE order (the order
# this tuple declares is also the order the admin latency panel lists steps in, T13.8.2) -- a
# (stage, step) pair outside it is REJECTED (``observe_step`` raises), never silently emitted,
# so this histogram can never grow a query-text/user-id/high-cardinality label by a future
# careless call site. Every call site in retrieval.py/reranking.py/generation.py uses only a
# literal pair from here, so the raise is a dev-time bug signal, never a real-traffic outcome.
# DA-14-B2 finding 5 / 14-B2-fix: both entries below were flagged as possibly dead after
# block 14-B2 replaced the live `classify_intent` step with the shared understanding step
# (`rag_app.assistant.core.understand`). Checked (grep across backend/, frontend/, docs/)
# before touching either, per this fix block's own LOW item:
# - ("classifying", "classify_intent"): no backend call site observes this (stage, step)
#   pair any more (`understand()` is only timed at the coarser "classify" STAGE level,
#   T13.8.1's documented mapping choice) -- it IS dead on the backend side. KEPT anyway: the
#   frontend's `LatencyPanel.STEP_LABELS["classify_intent"]` (an admin-UI label) and the
#   `admin-dashboard.spec.ts` e2e fixture both still reference the literal string. Removing
#   the allow-list entry would not break either (they are static/mocked, not read from this
#   module), but a future admin-latency payload CAN still legitimately carry this pair if a
#   later block re-adds fine-grained understanding-step timing (14.5, self-reported in the
#   14-B2 ADR) -- keeping the slot reserved avoids re-litigating this exact grep later.
# - ("responding", "stream_canned_reply"): NOT dead -- `generation.answer_question_stream`'s
#   chit-chat branch still calls `timing.step("responding", "stream_canned_reply")` on every
#   real chit-chat turn (the name predates P14-D1's "the reply is LLM-generated, not canned"
#   change, so it is misleading, but it is live wiring, not a stale leftover). Renaming it
#   would also require updating the frontend's `LatencyPanel.STEP_LABELS` entry and the
#   `admin-dashboard.spec.ts`/`adminPipeline.test.ts` fixtures that assert on the literal
#   string -- out of scope for this LOW item; tracked as a naming cleanup, not acted on here.
STEP_ALLOWLIST: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("classifying", ("classify_intent",)),
    ("retrieving", ("embed_query", "vector_search", "bm25_search", "rrf_fusion", "load_chunks")),
    ("reranking", ("wait_slot", "cross_encoder", "order_by_scores")),
    ("generating", ("build_prompt", "llm_first_token", "token_stream", "parse_answer")),
    ("checking", ("build_check_prompt", "llm_verdict", "parse_verdict")),
    ("responding", ("stream_canned_reply",)),
)

_STEP_ALLOWLIST_LOOKUP: dict[str, frozenset[str]] = {
    stage_name: frozenset(steps) for stage_name, steps in STEP_ALLOWLIST
}


class UnknownPipelineStepError(ValueError):
    """Raised by :func:`observe_step` for a (stage, step) pair outside ``STEP_ALLOWLIST``."""


STEP_SECONDS = Histogram(
    "secrag_chat_step_seconds",
    "Duration of one atomic step within a chat-pipeline stage (seconds), labelled by the"
    " fixed stage/step vocabulary in STEP_ALLOWLIST (T13.8.1).",
    ["stage", "step"],
    registry=REGISTRY,
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

# T13.5.1: the reviewed, explicit allow-list of label NAMES any metric in this module may
# ever declare -- every per-metric no-PII-label test above/below (and in
# ``backend/tests/test_metrics.py``) only catches a label on a metric someone remembered to
# write a test for; this allow-list + ``check_no_pii_labels`` instead catches metric N+1
# automatically, the moment a new ``Counter``/``Gauge``/``Histogram`` module-level object
# declares a label name that was never reviewed, without anyone having to add a new test.
SAFE_METRIC_LABELS: frozenset[str] = frozenset(
    {"stage", "step", "result", "kind", "route_group", "reason"}
)


def check_no_pii_labels(namespace: dict[str, object] | None = None) -> list[str]:
    """Registry-wide no-PII-label check (T13.5.1). Walks every object in ``namespace`` (this
    module's own globals by default -- every production caller) and, for anything exposing a
    ``prometheus_client`` metric's private ``_labelnames`` tuple (every ``Counter``/``Gauge``/
    ``Histogram`` has one, labelled or not), flags any label name not in
    ``SAFE_METRIC_LABELS``. A test passes a throwaway ``dict`` with a deliberately unsafe
    label instead, so it never has to mutate the real module-level metrics to prove this
    catches something."""
    if namespace is None:
        namespace = globals()
    problems: list[str] = []
    for name, obj in namespace.items():
        labelnames = getattr(obj, "_labelnames", None)
        if not labelnames:
            continue
        for label in labelnames:
            if label not in SAFE_METRIC_LABELS:
                problems.append(
                    f"metric '{name}': label '{label}' is not in the reviewed"
                    " SAFE_METRIC_LABELS allow-list (possible PII -- review before adding)"
                )
    return problems


_lock = threading.Lock()
_server: WSGIServer | None = None
_server_port: int | None = None


def observe_stage(stage: str, elapsed_s: float) -> None:
    """Record one stage duration (``rag_app.timing``'s ``on_stage`` callback)."""
    STAGE_SECONDS.labels(stage=stage).observe(elapsed_s)


def observe_step(stage: str, step: str, elapsed_s: float) -> None:
    """Record one atomic-step duration (``rag_app.timing``'s ``on_step`` callback, T13.8.1).

    Raises :class:`UnknownPipelineStepError` for any ``(stage, step)`` pair outside
    ``STEP_ALLOWLIST`` -- rejected, never emitted (see that constant's docstring)."""
    allowed = _STEP_ALLOWLIST_LOOKUP.get(stage)
    if allowed is None or step not in allowed:
        raise UnknownPipelineStepError(
            f"({stage!r}, {step!r}) is not in metrics.STEP_ALLOWLIST -- refusing to emit it"
        )
    STEP_SECONDS.labels(stage=stage, step=step).observe(elapsed_s)


@contextmanager
def track_in_flight() -> Iterator[None]:
    """Count one chat request (streaming or not) as in-flight for its whole duration."""
    CHAT_IN_FLIGHT.inc()
    try:
        yield
    finally:
        CHAT_IN_FLIGHT.dec()


def _quantile_from_buckets(
    buckets: list[tuple[float, float]], total: float, q: float
) -> float | None:
    """Linear interpolation of the ``q`` quantile (e.g. 0.5, 0.95) from a cumulative
    histogram's buckets -- the same approximation Prometheus's own ``histogram_quantile()``
    PromQL function uses, done by hand here so the admin latency panel (T13.2.5) gets the
    SAME computation whether it is reading the local compose stack or an Azure replica's own
    in-process histogram (R6-4: there is no managed Prometheus on Azure to run
    ``histogram_quantile()`` against at all) -- one code path, not two. ``buckets`` is
    ``[(le, cumulative_count), ...]`` sorted ascending by ``le``, the last one ``+Inf``.
    ``None`` when there are no observations yet (``total == 0``)."""
    if total <= 0:
        return None
    target = q * total
    prev_le, prev_count = 0.0, 0.0
    for le, cumulative in buckets:
        if cumulative >= target:
            if le == float("inf") or cumulative == prev_count:
                return prev_le  # cannot interpolate past the last finite bucket, or a flat run
            fraction = (target - prev_count) / (cumulative - prev_count)
            return prev_le + fraction * (le - prev_le)
        prev_le, prev_count = le, cumulative
    return prev_le  # pragma: no cover - unreachable: the +Inf bucket always covers `target`


def stage_latency_summary(
    registry: CollectorRegistry = REGISTRY, histogram_name: str = "secrag_chat_stage_seconds"
) -> dict[str, dict[str, float | None]]:
    """T13.2.5 (latency panel): p50/p95/count/avg per pipeline stage, read directly from
    ``STAGE_SECONDS``'s own bucket/count/sum samples in THIS process's registry -- never
    through ``MetricsBackend`` (a histogram's percentiles are not an "unlabelled
    counter/gauge", so that interface's ``current_value`` contract does not fit; a remote
    PromQL ``histogram_quantile()`` query would also need its own separate code path on
    Azure, which has no managed Prometheus to query -- R6-4). Same computation, same
    one-process-only caveat as the rest of Phase 13's Azure metrics story: "this replica's
    own stage timings since it last started", never cross-replica or historical.

    ``registry``/``histogram_name`` default to the real process-wide ones (every production
    caller); a test passes its own throwaway ``CollectorRegistry`` instead, so it never has
    to share state with every other test observing ``rag_app.metrics.STAGE_SECONDS`` in the
    same pytest session."""
    buckets: dict[str, list[tuple[float, float]]] = {}
    sums: dict[str, float] = {}
    counts: dict[str, float] = {}
    for metric in registry.collect():
        if metric.name != histogram_name:
            continue
        for sample in metric.samples:
            stage = sample.labels.get("stage")
            if stage is None:
                continue
            if sample.name.endswith("_bucket"):
                le = float(sample.labels["le"])
                buckets.setdefault(stage, []).append((le, sample.value))
            elif sample.name.endswith("_sum"):
                sums[stage] = sample.value
            elif sample.name.endswith("_count"):
                counts[stage] = sample.value
    out: dict[str, dict[str, float | None]] = {}
    for stage, stage_buckets in buckets.items():
        stage_buckets.sort(key=lambda item: item[0])
        count = counts.get(stage, 0.0)
        total = sums.get(stage, 0.0)
        out[stage] = {
            "count": count,
            "avg_seconds": (total / count) if count > 0 else None,
            "p50_seconds": _quantile_from_buckets(stage_buckets, count, 0.5),
            "p95_seconds": _quantile_from_buckets(stage_buckets, count, 0.95),
        }
    return out


def step_latency_summary(
    registry: CollectorRegistry = REGISTRY, histogram_name: str = "secrag_chat_step_seconds"
) -> dict[tuple[str, str], dict[str, float | None]]:
    """T13.8.2 (pipeline waterfall panel): the :func:`stage_latency_summary` computation,
    keyed by ``(stage, step)`` instead of a single ``stage`` label -- same bucket/count/sum
    read, same one-process-only caveat, same test-injected-registry convention."""
    buckets: dict[tuple[str, str], list[tuple[float, float]]] = {}
    sums: dict[tuple[str, str], float] = {}
    counts: dict[tuple[str, str], float] = {}
    for metric in registry.collect():
        if metric.name != histogram_name:
            continue
        for sample in metric.samples:
            stage = sample.labels.get("stage")
            step = sample.labels.get("step")
            if stage is None or step is None:
                continue
            key = (stage, step)
            if sample.name.endswith("_bucket"):
                le = float(sample.labels["le"])
                buckets.setdefault(key, []).append((le, sample.value))
            elif sample.name.endswith("_sum"):
                sums[key] = sample.value
            elif sample.name.endswith("_count"):
                counts[key] = sample.value
    out: dict[tuple[str, str], dict[str, float | None]] = {}
    for key, key_buckets in buckets.items():
        key_buckets.sort(key=lambda item: item[0])
        count = counts.get(key, 0.0)
        total = sums.get(key, 0.0)
        out[key] = {
            "count": count,
            "avg_seconds": (total / count) if count > 0 else None,
            "p50_seconds": _quantile_from_buckets(key_buckets, count, 0.5),
            "p95_seconds": _quantile_from_buckets(key_buckets, count, 0.95),
        }
    return out


def start_metrics_server(port: int, addr: str = "0.0.0.0") -> int:  # noqa: S104 - container-internal port, not published to the host (compose.yml)
    """Start the metrics HTTP server once per process; idempotent (safe to call from every
    ``lifespan`` start, incl. repeated ``TestClient(create_app())`` uses in tests).

    ``port=0`` lets the OS assign a free port (tests); returns the port actually bound.
    """
    global _server, _server_port
    with _lock:
        if _server is not None:
            assert _server_port is not None
            return _server_port
        server, _thread = start_http_server(port, addr=addr, registry=REGISTRY)
        _server = server
        _server_port = server.server_port
        return _server_port
