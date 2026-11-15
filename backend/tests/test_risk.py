"""Unit tests for signup risk scoring and the velocity tracker."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_app import metrics
from rag_app.api.app import create_app
from rag_app.risk import (
    SignupTracker,
    is_disposable,
    is_high_risk,
    signup_risk_score,
)


def test_disposable_detection() -> None:
    assert is_disposable("a@mailinator.com") is True
    assert is_disposable("a@gmail.com") is False


def test_disposable_email_is_high_risk() -> None:
    assert is_high_risk(signup_risk_score("a@mailinator.com", 0)) is True


def test_velocity_is_high_risk() -> None:
    assert is_high_risk(signup_risk_score("a@gmail.com", 5)) is True
    assert is_high_risk(signup_risk_score("a@gmail.com", 0)) is False


def test_tracker_sliding_window() -> None:
    tracker = SignupTracker(window_seconds=100)
    tracker.record("1.2.3.4", now=0.0)
    tracker.record("1.2.3.4", now=10.0)
    assert tracker.count("1.2.3.4", now=20.0) == 2
    assert tracker.count("1.2.3.4", now=200.0) == 0  # both outside the window


def test_register_with_a_disposable_email_is_blocked_and_counted() -> None:
    """T13.0.6: the risk check runs before any DB work (``rag_app.api.auth.register``'s
    first lines), so this needs no live database — the SAME reasoning
    ``test_csp_report.py`` already relies on for its own DB-free rate-limit tests. Reason
    label is bounded (``disposable_email``/``signup_velocity``), never the address."""
    client = TestClient(create_app())
    before = metrics.SIGNUP_BLOCKED_TOTAL.labels(reason="disposable_email")._value.get()
    response = client.post(
        "/auth/register",
        json={"email": "attacker@mailinator.com", "password": "C0rrect!Horse99Battery"},
    )
    assert response.status_code == 403
    after = metrics.SIGNUP_BLOCKED_TOTAL.labels(reason="disposable_email")._value.get()
    assert after == before + 1
