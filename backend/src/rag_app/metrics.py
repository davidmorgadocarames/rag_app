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

from prometheus_client import CollectorRegistry, Gauge, Histogram, start_http_server

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
