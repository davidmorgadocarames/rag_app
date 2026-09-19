"""Unit tests for signup risk scoring and the velocity tracker."""

from __future__ import annotations

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
