"""Prometheus metrics for the chat pipeline (T11.3.2, TF4).

Served on a **separate internal port** (``Settings.metrics_port``), never the API port:
mixing them would expose route/volume/error shape on a port reachable from the browser.
Only the API's own lifespan starts this server (``start_metrics_server``); the FastAPI
app in ``rag_app.api.app`` never registers a ``/metrics`` route itself.

Histograms carry only the ``stage`` label (a small, fixed vocabulary: classify, embed,
hybrid_search, rerank_load, rerank_inference, generate, groundedness, total, ttft) — never
a user id, IP, question text or answer text (see PHASE_PLANNING appendix "Prometheus in
one page"). The in-flight gauge has no labels at all.
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
#   route_group  chat / login / email / csp_report / account / mfa (RATE_LIMIT_HITS_TOTAL)
#   reason       disposable_email / signup_velocity (SIGNUP_BLOCKED_TOTAL)
#   result       success / failure (MFA_STEP_UP_TOTAL)
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

_lock = threading.Lock()
_server: WSGIServer | None = None
_server_port: int | None = None


def observe_stage(stage: str, elapsed_s: float) -> None:
    """Record one stage duration (``rag_app.timing``'s ``on_stage`` callback)."""
    STAGE_SECONDS.labels(stage=stage).observe(elapsed_s)


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
