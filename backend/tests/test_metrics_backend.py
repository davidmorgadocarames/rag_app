"""``MetricsBackend`` local implementation (T13.1.3): unit tests, no live Prometheus (the
gate's ``compose.gate.yml`` project does not run one -- see ``metrics_backend.py``'s module
docstring); every case below proves the "never raises, degrade to None" contract instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from rag_app import metrics_backend as mb

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry


def test_an_empty_base_url_never_attempts_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_get(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    backend = mb.PrometheusMetricsBackend(base_url="")
    assert backend.current_value("secrag_lockout_hits_total") is None
    assert called is False


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def test_a_real_value_is_parsed_from_the_instant_query_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, params: dict[str, str], timeout: float) -> _FakeResponse:
        assert params["query"] == "sum(secrag_lockout_hits_total)"
        return _FakeResponse({"data": {"result": [{"value": [1700000000, "3"]}]}})

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    backend = mb.PrometheusMetricsBackend(base_url="http://prometheus:9090")
    assert backend.current_value("secrag_lockout_hits_total") == 3.0


def test_an_empty_result_is_unknown_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: object, **kwargs: object) -> _FakeResponse:
        return _FakeResponse({"data": {"result": []}})

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    backend = mb.PrometheusMetricsBackend(base_url="http://prometheus:9090")
    assert backend.current_value("secrag_never_scraped_total") is None


def test_an_unreachable_server_degrades_to_none_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(*args: object, **kwargs: object) -> None:
        raise ConnectionError("no route to host")

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    backend = mb.PrometheusMetricsBackend(base_url="http://prometheus:9090")
    assert backend.current_value("secrag_lockout_hits_total") is None


def test_a_malformed_payload_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: object, **kwargs: object) -> _FakeResponse:
        return _FakeResponse({"data": {"result": [{"value": ["not-a-number"]}]}})

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    backend = mb.PrometheusMetricsBackend(base_url="http://prometheus:9090")
    assert backend.current_value("secrag_lockout_hits_total") is None


def test_current_values_queries_every_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_get(url: str, params: dict[str, str], timeout: float) -> _FakeResponse:
        seen.append(params["query"])
        return _FakeResponse({"data": {"result": [{"value": [0, "1"]}]}})

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    backend = mb.PrometheusMetricsBackend(base_url="http://prometheus:9090")
    values = backend.current_values(["a_total", "b_total"])
    assert values == {"a_total": 1.0, "b_total": 1.0}
    assert seen == ["sum(a_total)", "sum(b_total)"]


def test_get_metrics_backend_reads_the_current_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMETHEUS_URL", "http://example-prometheus:9090")
    backend = mb.get_metrics_backend()
    assert isinstance(backend, mb.PrometheusMetricsBackend)
    assert backend.base_url == "http://example-prometheus:9090"


def test_get_metrics_backend_falls_back_to_in_process_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T13.4.3 (R6-4): no managed Prometheus on Azure -- PROMETHEUS_URL stays unset there, so
    the dependency factory must hand back the in-process implementation, not silently
    degrade every metric to "unknown"."""
    monkeypatch.setenv("PROMETHEUS_URL", "")
    backend = mb.get_metrics_backend()
    assert isinstance(backend, mb.InProcessMetricsBackend)


# --- InProcessMetricsBackend (T13.4.3, Azure: no managed Prometheus, R6-4) -----------------


def _registry_with(counter_total: float, gauge_value: float) -> CollectorRegistry:
    from prometheus_client import CollectorRegistry, Counter, Gauge

    registry = CollectorRegistry()
    counter = Counter("fake_counter_total", "test", registry=registry)
    counter.inc(counter_total)
    gauge = Gauge("fake_gauge", "test", registry=registry)
    gauge.set(gauge_value)
    return registry


def test_in_process_backend_reads_a_counter_and_a_gauge_from_its_own_registry() -> None:
    backend = mb.InProcessMetricsBackend(registry=_registry_with(3, 7))
    assert backend.current_value("fake_counter_total") == 3.0
    assert backend.current_value("fake_gauge") == 7.0


def test_in_process_backend_sums_across_label_combinations() -> None:
    from prometheus_client import CollectorRegistry, Counter

    registry = CollectorRegistry()
    counter = Counter("fake_labelled_total", "test", ["kind"], registry=registry)
    counter.labels(kind="a").inc(2)
    counter.labels(kind="b").inc(5)
    backend = mb.InProcessMetricsBackend(registry=registry)
    # Matches PrometheusMetricsBackend's own sum(metric_name) PromQL semantics.
    assert backend.current_value("fake_labelled_total") == 7.0


def test_in_process_backend_reports_an_unregistered_metric_as_unknown_not_zero() -> None:
    backend = mb.InProcessMetricsBackend(registry=_registry_with(0, 0))
    assert backend.current_value("secrag_never_registered_total") is None


def test_in_process_backend_defaults_to_the_real_process_registry() -> None:
    from rag_app import metrics as real_metrics

    real_metrics.LOCKOUT_HITS_TOTAL.inc()
    before = real_metrics.REGISTRY.get_sample_value("secrag_lockout_hits_total") or 0.0
    backend = mb.InProcessMetricsBackend()
    assert backend.current_value("secrag_lockout_hits_total") == before


def test_in_process_backend_current_values_queries_every_name() -> None:
    backend = mb.InProcessMetricsBackend(registry=_registry_with(3, 7))
    values = backend.current_values(["fake_counter_total", "fake_gauge", "unknown_metric"])
    assert values == {"fake_counter_total": 3.0, "fake_gauge": 7.0, "unknown_metric": None}


# --- Contract: both implementations return the SAME SHAPE for the SAME input (T13.4.3) ----


def test_both_backends_return_the_same_shape_for_the_same_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, params: dict[str, str], timeout: float) -> _FakeResponse:
        # Discriminates by query, like the real Prometheus server would: "unknown_metric"
        # genuinely has no series, exactly like the in-process registry below.
        if params["query"] == "sum(fake_counter_total)":
            return _FakeResponse({"data": {"result": [{"value": [0, "3"]}]}})
        return _FakeResponse({"data": {"result": []}})

    monkeypatch.setattr(mb.httpx, "get", fake_get)
    prometheus_backend = mb.PrometheusMetricsBackend(base_url="http://prometheus:9090")
    in_process_backend = mb.InProcessMetricsBackend(registry=_registry_with(3, 3))

    names = ["fake_counter_total", "unknown_metric"]
    prometheus_values = prometheus_backend.current_values(names)
    in_process_values = in_process_backend.current_values(names)
    expected = {"fake_counter_total": 3.0, "unknown_metric": None}
    assert set(prometheus_values) == set(in_process_values) == set(names)
    assert prometheus_values == in_process_values == expected
