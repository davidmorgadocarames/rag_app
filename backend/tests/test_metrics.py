"""Unit tests for Prometheus metrics (T11.3.2, TF4): separate port, stage-only labels,
in-flight gauge, no PII.
"""

from __future__ import annotations

import httpx
import pytest

from rag_app import metrics


def test_stage_histogram_has_only_a_stage_label() -> None:
    """No-PII-label test: never a user id, IP, question or answer — just ``stage``."""
    assert metrics.STAGE_SECONDS._labelnames == ("stage",)


def test_in_flight_gauge_has_no_labels() -> None:
    assert metrics.CHAT_IN_FLIGHT._labelnames == ()


def test_track_in_flight_increments_and_decrements() -> None:
    before = metrics.CHAT_IN_FLIGHT._value.get()
    with metrics.track_in_flight():
        assert metrics.CHAT_IN_FLIGHT._value.get() == before + 1
    assert metrics.CHAT_IN_FLIGHT._value.get() == before


def test_track_in_flight_decrements_even_on_exception() -> None:
    before = metrics.CHAT_IN_FLIGHT._value.get()
    with pytest.raises(RuntimeError):
        with metrics.track_in_flight():
            raise RuntimeError("boom")
    assert metrics.CHAT_IN_FLIGHT._value.get() == before


def test_metrics_server_serves_observed_stages_on_its_own_port() -> None:
    port = metrics.start_metrics_server(0, addr="127.0.0.1")
    metrics.observe_stage("rerank_inference", 0.123)
    body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    assert 'secrag_chat_stage_seconds_bucket{le="0.25",stage="rerank_inference"}' in body
    assert "secrag_chat_requests_in_flight" in body
    # No PII ever reaches a metric label or help text.
    assert "question" not in body.lower()
    assert "@" not in body


def test_start_metrics_server_is_idempotent() -> None:
    first = metrics.start_metrics_server(0, addr="127.0.0.1")
    second = metrics.start_metrics_server(0, addr="127.0.0.1")
    assert first == second


def test_api_port_never_serves_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API app itself registers no /metrics route — it lives on a separate port only."""
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app

    client = TestClient(create_app())  # no lifespan: metrics server is irrelevant here
    res = client.get("/metrics")
    assert res.status_code == 404
