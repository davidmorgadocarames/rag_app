"""``MetricsBackend`` local implementation (T13.1.3): unit tests, no live Prometheus (the
gate's ``compose.gate.yml`` project does not run one -- see ``metrics_backend.py``'s module
docstring); every case below proves the "never raises, degrade to None" contract instead.
"""

from __future__ import annotations

import pytest

from rag_app import metrics_backend as mb


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
