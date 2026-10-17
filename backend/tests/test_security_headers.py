"""Backend surface reduction (T12.2.4, E6): docs/redoc/openapi gated to ``ENV=dev``,
security headers on every API response, CORS narrowed to the old frontend's rollback needs
(single origin, GET/POST/PATCH/DELETE, ``Authorization``/``Content-Type``, no credentials).

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


# --- T12.2.4 / F8: CORS narrowed to the old frontend's rollback needs -----------------------


def test_cors_narrowed_to_the_old_frontends_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    client = TestClient(create_app())
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    allowed_methods = {
        m.strip() for m in response.headers["access-control-allow-methods"].split(",")
    }
    # Narrowed: no PUT-via-wildcard surprise, no wildcard either.
    assert allowed_methods == {"GET", "POST", "PATCH", "DELETE"}
    allowed_headers = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    # Starlette's CORSMiddleware always adds the CORS-safelisted simple headers (accept,
    # accept-language, content-language) on top of our own list.
    assert {"authorization", "content-type"} <= allowed_headers
    # No credentials: the old frontend sends a Bearer token, never a cookie.
    assert "access-control-allow-credentials" not in response.headers


def test_cors_preflight_rejects_a_header_outside_the_narrowed_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starlette's ``CORSMiddleware`` fails the WHOLE preflight (400) the moment a
    requested header is not on the allow-list -- this is what "narrowed" actually buys over
    the old ``allow_headers=["*"]`` (F8), which echoed back anything asked for."""
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    client = TestClient(create_app())
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with",
        },
    )
    assert response.status_code == 400


def test_cors_rejects_a_foreign_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    client = TestClient(create_app())
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers
