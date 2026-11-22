"""``GET /wake`` (T13.13.2, DEBT-061): unauthenticated, rate-limited per IP, always a plain
204 (no body, no leak either way), triggers ``rag_app.warmup``'s single-flight guard.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_app import metrics, warmup
from rag_app.api.app import create_app
from rag_app.api.deps import get_rate_limiter
from rag_app.features import iter_routes
from rag_app.ratelimit import RateLimiter


def _client() -> TestClient:
    return TestClient(create_app())


def test_wake_is_unauthenticated_and_returns_204_with_no_body() -> None:
    response = _client().get("/wake")
    assert response.status_code == 204
    assert response.content == b""


def test_wake_triggers_the_single_flight_warm_up(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr("rag_app.wake.warmup.trigger_warm_up", lambda *a: calls.append(a) or True)
    response = _client().get("/wake")
    assert response.status_code == 204
    assert calls == [()]


def test_wake_never_leaks_whether_a_warm_up_was_skipped_or_started(monkeypatch) -> None:
    """Same response either way -- a caller learns nothing about Ollama's internal state."""
    monkeypatch.setattr("rag_app.wake.warmup.trigger_warm_up", lambda *a: False)
    skipped = _client().get("/wake")
    monkeypatch.setattr("rag_app.wake.warmup.trigger_warm_up", lambda *a: True)
    started = _client().get("/wake")
    assert skipped.status_code == started.status_code == 204
    assert skipped.content == started.content == b""


def test_wake_tolerates_a_failing_warm_up_trigger(monkeypatch) -> None:
    """``trigger_warm_up`` itself never raises (``rag_app.warmup``'s own contract), but the
    route catches it anyway (defense in depth) -- a caller must still get 204, never a 500
    that would hint at backend internals."""

    def _boom(*_a: object) -> bool:
        raise RuntimeError("should never happen, but the route must not 500 on it either")

    monkeypatch.setattr("rag_app.wake.warmup.trigger_warm_up", _boom)
    response = _client().get("/wake")
    assert response.status_code == 204
    assert response.content == b""


def test_wake_rate_limited_per_ip(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(warmup, "trigger_warm_up", lambda *a: True)
    # The SAME instance on every call (see test_csp_report.py's identical pattern): a fresh
    # one per request would never let a bucket actually empty across requests.
    shared_limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    app.dependency_overrides[get_rate_limiter] = lambda: shared_limiter
    client = TestClient(app)
    first = client.get("/wake")
    second = client.get("/wake")
    assert first.status_code == 204
    assert second.status_code == 429


def test_wake_rate_limit_hit_increments_the_wake_route_group(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(warmup, "trigger_warm_up", lambda *a: True)
    shared_limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    app.dependency_overrides[get_rate_limiter] = lambda: shared_limiter
    client = TestClient(app)
    before = metrics.RATE_LIMIT_HITS_TOTAL.labels(route_group="wake")._value.get()
    client.get("/wake")
    second = client.get("/wake")
    assert second.status_code == 429
    after = metrics.RATE_LIMIT_HITS_TOTAL.labels(route_group="wake")._value.get()
    assert after == before + 1


def test_wake_is_registered_tagged_stack() -> None:
    app = create_app()
    matches = [r for r in iter_routes(app) if getattr(r, "path", None) == "/wake"]
    assert matches, "GET /wake must be registered on the app"
    assert "stack" in (matches[0].tags or [])
