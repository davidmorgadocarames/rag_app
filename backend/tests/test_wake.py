"""``GET /wake`` (T13.13.2, DEBT-061; DEC-050/T13.J4-fix narrowed its scope): unauthenticated,
rate-limited per IP through its OWN tight bucket, always a plain 204, and -- since DEC-050 --
NEVER triggers the Ollama warm-up any more (that only happens after a successful login or an
authenticated /chat page load; see test_auth.py / test_conversations.py and test_warmup.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag_app import metrics, warmup
from rag_app.api import deps as deps_module
from rag_app.api.app import create_app
from rag_app.api.deps import get_wake_rate_limiter
from rag_app.config import get_settings
from rag_app.features import iter_routes
from rag_app.ratelimit import RateLimiter


@pytest.fixture(autouse=True)
def _reset_wake_rate_limiter() -> None:
    """DEC-050's own bucket is deliberately tiny (default capacity 1) and a process-wide
    singleton (same lazy pattern as ``get_rate_limiter``) -- without a reset, whichever test
    in this module runs first against the REAL (non-overridden) limiter would permanently
    exhaust the one token for every later test in the same session."""
    deps_module._wake_rate_limiter = None


def _client() -> TestClient:
    return TestClient(create_app())


def test_wake_is_unauthenticated_and_returns_204_with_no_body() -> None:
    response = _client().get("/wake")
    assert response.status_code == 204
    assert response.content == b""


def test_wake_never_triggers_the_ollama_warm_up(monkeypatch) -> None:
    """DEC-050 (DA-13-J3J4-5): the whole point of this fix -- an unauthenticated /wake call
    must never reach ``rag_app.warmup.trigger_warm_up`` any more. Negative control: red on
    the pre-fix code, which called it unconditionally."""
    calls: list[object] = []
    monkeypatch.setattr(warmup, "trigger_warm_up", lambda *a: calls.append(a) or True)
    response = _client().get("/wake")
    assert response.status_code == 204
    assert calls == [], "an unauthenticated /wake call must never trigger the Ollama warm-up"


def test_wake_rate_limited_per_ip_through_its_own_bucket(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(warmup, "trigger_warm_up", lambda *a: True)
    # The SAME instance on every call (see test_csp_report.py's identical pattern): a fresh
    # one per request would never let a bucket actually empty across requests. Overriding
    # `get_wake_rate_limiter` (DEC-050's own dedicated dependency) -- never the generic
    # `get_rate_limiter`, which /wake no longer depends on at all.
    shared_limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    app.dependency_overrides[get_wake_rate_limiter] = lambda: shared_limiter
    client = TestClient(app)
    first = client.get("/wake")
    second = client.get("/wake")
    assert first.status_code == 204
    assert second.status_code == 429


def test_wake_rate_limit_hit_increments_the_wake_route_group(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(warmup, "trigger_warm_up", lambda *a: True)
    shared_limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    app.dependency_overrides[get_wake_rate_limiter] = lambda: shared_limiter
    client = TestClient(app)
    before = metrics.RATE_LIMIT_HITS_TOTAL.labels(route_group="wake")._value.get()
    client.get("/wake")
    second = client.get("/wake")
    assert second.status_code == 429
    after = metrics.RATE_LIMIT_HITS_TOTAL.labels(route_group="wake")._value.get()
    assert after == before + 1


def test_wake_default_bucket_is_far_tighter_than_the_generic_one() -> None:
    """DEC-050's own Done-when: /wake's bucket (default 1 req/60s) must not be the generic
    60-capacity/1-per-second bucket every other unauthenticated route uses."""
    settings = get_settings()
    assert settings.wake_rate_limit_capacity <= 1
    assert settings.wake_rate_limit_refill_per_second <= 1.0 / 60.0
    assert (settings.wake_rate_limit_capacity, settings.wake_rate_limit_refill_per_second) != (
        settings.rate_limit_capacity,
        settings.rate_limit_refill_per_second,
    )


def test_wake_is_registered_tagged_stack() -> None:
    app = create_app()
    matches = [r for r in iter_routes(app) if getattr(r, "path", None) == "/wake"]
    assert matches, "GET /wake must be registered on the app"
    assert "stack" in (matches[0].tags or [])
