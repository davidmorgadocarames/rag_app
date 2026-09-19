"""Signup risk scoring (anti-Sybil).

Stacks simple signals — disposable-email domains and per-IP signup velocity — into a
score; a high score blocks the signup. Deliberately small and pure so the scoring is
unit-testable; the velocity tracker is in-memory (Redis in a multi-instance deployment).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

_DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "tempmail.com",
        "10minutemail.com",
        "guerrillamail.com",
        "trashmail.com",
        "yopmail.com",
        "temp-mail.org",
        "throwaway.email",
        "getnada.com",
        "dispostable.com",
    }
)

_HIGH_RISK = 100


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_disposable(email: str) -> bool:
    return email_domain(email) in _DISPOSABLE_DOMAINS


def signup_risk_score(email: str, recent_signups_from_ip: int) -> int:
    """Higher = riskier. >= 100 is blocked."""
    score = 0
    if is_disposable(email):
        score += 100
    if recent_signups_from_ip >= 5:
        score += 100
    elif recent_signups_from_ip >= 3:
        score += 40
    return score


def is_high_risk(score: int) -> bool:
    return score >= _HIGH_RISK


class SignupTracker:
    """In-memory per-IP signup timestamps within a sliding window."""

    def __init__(self, window_seconds: float = 3600.0) -> None:
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> None:
        cutoff = now - self.window_seconds
        self._events[ip] = [t for t in self._events[ip] if t >= cutoff]

    def count(self, ip: str, now: float | None = None) -> int:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._prune(ip, moment)
            return len(self._events[ip])

    def record(self, ip: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._events[ip].append(moment)
