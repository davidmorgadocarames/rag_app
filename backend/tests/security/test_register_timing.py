"""DA-12bB-1 (T12.4.4, F3/OWASP-A07): ``/auth/register`` must cost the SAME whether the
address already has an account or not -- not just return the same body. Mirrors
``backend/tests/test_security.py::test_login_timing_does_not_distinguish_a_real_account_from_a_missing_one``
but at the route level, since the discriminating cost here is the whole register handler
(the argon2 hash plus the attempted DB writes), not a single function call.
"""

from __future__ import annotations

import time
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rag_app.api.app import create_app
from rag_app.api.deps import get_signup_tracker
from rag_app.risk import SignupTracker

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("LOCKOUT_HMAC_SECRET", "l" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")  # best-effort emailer: no real network, no jitter
    return db_engine


def _client() -> TestClient:
    app = create_app()
    # A FRESH SignupTracker per request (never shared across the repeated calls below): the
    # repeated register attempts this test makes would otherwise trip the per-IP signup
    # velocity risk check (rag_app.risk, >= 5 recent signups from the same address) long
    # before the timing measurement completes -- same override as
    # backend/tests/security/test_client_tampering.py.
    app.dependency_overrides[get_signup_tracker] = SignupTracker
    return TestClient(app, base_url=BASE_URL)


def _password() -> str:
    return "correct horse battery staple " + uuid.uuid4().hex[:6]


def _new_email() -> str:
    return f"register-timing-{uuid.uuid4().hex[:12]}@example.test"


def test_register_timing_does_not_distinguish_an_existing_account_from_a_new_one(
    api_env: Engine,
) -> None:
    """DA-12bB-1: the existing-email branch (``IntegrityError`` on the unique index) must
    pay the same argon2-hash + DB-write cost as the brand-new-account branch. Repeats several
    times and compares averages (same tolerance approach as the login timing test): a
    regression to "no hash, no DB write on the existing branch" would make that branch's
    average close to instant -- a ratio nothing like the ~1x this test allows."""
    client = _client()
    existing_email = _new_email()
    register = client.post(
        "/auth/register", json={"email": existing_email, "password": _password()}
    )
    assert register.status_code == 202, register.text

    repeats = 6

    def _measure_existing() -> float:
        start = time.perf_counter()
        for _ in range(repeats):
            response = client.post(
                "/auth/register", json={"email": existing_email, "password": _password()}
            )
            assert response.status_code == 202, response.text
        return (time.perf_counter() - start) / repeats

    def _measure_new() -> float:
        start = time.perf_counter()
        for _ in range(repeats):
            response = client.post(
                "/auth/register", json={"email": _new_email(), "password": _password()}
            )
            assert response.status_code == 202, response.text
        return (time.perf_counter() - start) / repeats

    # Warm up (first argon2 call sometimes pays a one-off cost, e.g. CPU feature detection)
    # so neither measured loop below is skewed by whichever branch happens to run first.
    client.post("/auth/register", json={"email": _new_email(), "password": _password()})
    client.post("/auth/register", json={"email": existing_email, "password": _password()})

    existing_avg = _measure_existing()
    new_avg = _measure_new()
    ratio = max(existing_avg, new_avg) / min(existing_avg, new_avg)
    assert ratio < 2.0, (
        f"timing differs too much between an existing account ({existing_avg * 1000:.1f}"
        f" ms/call) and a new one ({new_avg * 1000:.1f} ms/call) -- an attacker could"
        " time-distinguish them (tolerance documented above: both must pay the same real"
        " argon2 + DB-write cost)"
    )
