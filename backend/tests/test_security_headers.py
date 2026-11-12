"""Backend surface reduction (T12.2.4, E6): docs/redoc/openapi gated to ``ENV=dev``,
security headers on every API response. CORS -- narrowed in 12a to the old frontend's
rollback needs -- was removed ENTIRELY in T12.5.3: the BFF-based frontend is the only
caller and never triggers a browser CORS preflight (same-origin, server-to-server).

No DB, no LLM: ``TestClient(create_app())`` used WITHOUT the ``with`` context manager never
runs the lifespan (Starlette only starts it on ``__enter__``), so these tests do not need the
full set of ``ENV=prod`` secrets (``CSRF_SECRET``, ``CLIENT_IP_HMAC_KEYS``, ...) -- the same
pattern ``test_api.py`` already relies on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag_app.api.app import create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # ENV defaults to "prod" (JobSettings): tests that want "dev" set it explicitly below.
    monkeypatch.delenv("ENV", raising=False)


# --- T12.2.4 / F7: /docs, /redoc, /openapi.json only with ENV=dev ---------------------------


def test_docs_routes_are_served_with_env_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_docs_routes_are_404_with_env_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- T12.2.4 / E6: security headers on every API response -----------------------------------


def test_security_headers_present_on_an_ordinary_response() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_security_headers_present_even_on_a_404() -> None:
    client = TestClient(create_app())
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


# --- T12.5.3 / F8: CORS removed entirely ------------------------------------------------------


def test_cors_is_removed_entirely_even_for_the_frontends_own_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``CORSMiddleware`` at all any more (contrast with 12a's narrowed-but-present
    allow-list): an ``OPTIONS`` preflight, even from the frontend's own configured origin,
    gets back a plain response with NO ``access-control-*`` header at all -- there is no
    middleware left to add one. (The BFF-based frontend never sends this preflight for
    real: same-origin, server-to-server -- this just proves the surface is gone.)"""
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    client = TestClient(create_app())
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert not any(h.lower().startswith("access-control-") for h in response.headers)


def test_cors_is_removed_entirely_for_a_foreign_origin_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    client = TestClient(create_app())
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert not any(h.lower().startswith("access-control-") for h in response.headers)
