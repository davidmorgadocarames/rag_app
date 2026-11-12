"""Exposed-files check (T12.5.7, user idea 2026-11-09, DEC-023): none of
``/.git/HEAD``, ``/.git/config``, ``/.env``, ``/.env.local``, a ``.next/`` server file, a
``*.map`` source map, ``/backend/`` or ``/docker-compose.yml`` is ever served with real
content. The backend has no static file serving at all (every route it answers is declared
explicitly, T12.5.2's route walker proves the full list) -- checked here directly. The
FRONTEND is the one surface a misconfigured static server could theoretically leak a repo
file through; ``scripts/azure/exposed_files_check.sh`` probes it (used by the Azure smoke,
T12.6b.6, and locally against any running frontend) -- its PASS/FAIL logic is proven here
against a real throwaway HTTP server standing in for the frontend, never a live one.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_app.api.app import create_app

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPOSED_FILES_CHECK = REPO_ROOT / "scripts" / "azure" / "exposed_files_check.sh"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Mirrors the script's own list exactly (one representative path per T12.5.7 category) --
# if the script's list ever drifts from this one, that is itself worth catching.
PATHS = [
    "/.git/HEAD",
    "/.git/config",
    "/.env",
    "/.env.local",
    "/.next/BUILD_ID",
    "/_next/static/chunks/main.js.map",
    "/backend/",
    "/docker-compose.yml",
]


# --- the backend itself never serves any of these ---------------------------------------


@pytest.mark.parametrize("path", PATHS)
def test_the_backend_never_serves_any_of_these_paths(path: str) -> None:
    """No lifespan (no DB/secrets needed): a 404 (or, for ``/backend/`` and
    ``/docker-compose.yml``, whatever a GET with no matching route answers) -- never a 200
    with content. The backend declares every route explicitly (T12.5.2); none of these is
    one of them."""
    client = TestClient(create_app())
    response = client.get(path)
    assert response.status_code != 200, f"{path} must never be served with real content"


# --- the smoke helper's own PASS/FAIL logic, against a real throwaway server ------------


class _PerPathHandler(BaseHTTPRequestHandler):
    """Answers 404 for every path EXCEPT the ones in ``leaky_paths`` (class attribute,
    mutable per test), which answer 200 with a body -- standing in for "the frontend
    accidentally serves this file for real"."""

    leaky_paths: set[str] = set()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path in self.leaky_paths:
            body = b"leaked content"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # silence per-request stderr noise
        pass


@pytest.fixture
def fake_frontend() -> Iterator[str]:
    """A real, throwaway HTTP server on 127.0.0.1 -- ``_PerPathHandler.leaky_paths`` is
    reset empty before each test, mutated by the test itself to simulate an exposure."""
    _PerPathHandler.leaky_paths = set()
    server = HTTPServer(("127.0.0.1", 0), _PerPathHandler)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(EXPOSED_FILES_CHECK), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


@needs_bash
def test_the_helper_passes_when_nothing_is_exposed(fake_frontend: str) -> None:
    proc = _run(fake_frontend)
    assert proc.returncode == 0, proc.stderr
    for path in PATHS:
        assert f"{fake_frontend}{path} -> 404 (ok)" in proc.stdout


@needs_bash
def test_the_helper_fails_closed_when_one_path_is_exposed(fake_frontend: str) -> None:
    """Proves the check has teeth: a single leaking path fails the WHOLE run (never
    averaged away by the other seven passing) and is named in the output."""
    _PerPathHandler.leaky_paths = {"/.env"}
    proc = _run(fake_frontend)
    assert proc.returncode == 1
    assert f"{fake_frontend}/.env -> 200 (EXPOSED)" in proc.stderr
    # Every OTHER path still reported fine -- the failure is specific, not a blanket one.
    for path in PATHS:
        if path == "/.env":
            continue
        assert f"{fake_frontend}{path} -> 404 (ok)" in proc.stdout


@needs_bash
def test_the_helper_fails_closed_when_every_path_is_exposed(fake_frontend: str) -> None:
    _PerPathHandler.leaky_paths = set(PATHS)
    proc = _run(fake_frontend)
    assert proc.returncode == 1
    for path in PATHS:
        assert f"{fake_frontend}{path} -> 200 (EXPOSED)" in proc.stderr


@needs_bash
def test_the_helper_requires_a_url_argument() -> None:
    assert _run().returncode == 2


@needs_bash
def test_the_helper_never_prints_a_response_body(fake_frontend: str) -> None:
    """No secrets rule: only the URL and the resulting HTTP status are ever printed."""
    _PerPathHandler.leaky_paths = {"/.env"}
    proc = _run(fake_frontend)
    assert "leaked content" not in proc.stdout
    assert "leaked content" not in proc.stderr
