"""Unit tests for ``rag_app.email_abuse.EmailAbuseLimiter`` (deterministic via explicit
``now``, same pattern as ``test_ratelimit.py``).

DA-12bC-1 / DEC-026 = A regression tests: a single attacker IP trickling requests for a
victim's address must never be able to starve that address for everyone (a), while a
many-IP flood still hits the pure-recipient backstop by design (b)."""

from __future__ import annotations

from rag_app.email_abuse import (
    _RECIPIENT_CAPACITY,
    _RECIPIENT_IP_CAPACITY,
    _RECIPIENT_IP_REFILL_PER_SECOND,
    EmailAbuseLimiter,
)

VICTIM = "victim@example.test"


def test_single_attacker_ip_trickling_never_starves_the_victim_from_another_ip() -> None:
    """The attacker first bursts its OWN ``(recipient, ip)`` bucket, then keeps requesting
    at the fastest rate that bucket allows (one token every ``1 / refill`` seconds) for a
    long simulated time -- the victim, from a DIFFERENT ip, must still get through at the
    end. This is exactly the DA-12bC-1 scenario: on the OLD code (one shared recipient
    bucket, 5/hour, no per-IP axis) this would fail once the attacker had drained it."""
    limiter = EmailAbuseLimiter()
    attacker_ip = "203.0.113.5"
    victim_ip = "198.51.100.9"
    now = 0.0

    # Burst the attacker's own (recipient, ip) bucket dry.
    for _ in range(int(_RECIPIENT_IP_CAPACITY)):
        assert limiter.allow("forgot", attacker_ip, VICTIM, now=now) is True
    assert limiter.allow("forgot", attacker_ip, VICTIM, now=now) is False

    # Sustained trickle at the attacker's own bucket's steady-state rate for a long time
    # (50 refills ~ 16.7 simulated hours at 3/hour) -- every one of these succeeds (the
    # attacker's own bucket lets it through), but never drains the backstop enough to block
    # the victim below.
    period = 1.0 / _RECIPIENT_IP_REFILL_PER_SECOND
    for _ in range(50):
        now += period
        assert limiter.allow("forgot", attacker_ip, VICTIM, now=now) is True

    # The victim, from a DIFFERENT ip, still gets through at any point during the trickle.
    assert limiter.allow("forgot", victim_ip, VICTIM, now=now) is True


def test_many_ip_flood_still_hits_the_recipient_backstop() -> None:
    """Defence in depth kept from the original design: enough DISTINCT attacker IPs, each
    well under its own ``(recipient, ip)`` cap, can still exhaust the pure-recipient
    backstop (accepted residual risk, see docs/security/THREAT_MODEL.md)."""
    limiter = EmailAbuseLimiter()
    now = 0.0
    allowed = 0
    for i in range(int(_RECIPIENT_CAPACITY) + 10):
        ip = f"203.0.113.{i}"
        if limiter.allow("forgot", ip, VICTIM, now=now):
            allowed += 1
    assert allowed == int(_RECIPIENT_CAPACITY)
    # One more, brand-new IP: its own (recipient, ip) bucket is fresh, but the backstop is
    # now empty.
    assert limiter.allow("forgot", "203.0.113.250", VICTIM, now=now) is False


def test_recipient_bucket_keys_never_contain_the_raw_email() -> None:
    """The recipient identity is HMACed with a per-process salt before it is used as an
    in-memory bucket key -- the raw email must never appear in either recipient-keyed
    bucket's key space."""
    limiter = EmailAbuseLimiter()
    distinctive_email = "do-not-leak-this-address@example.test"
    limiter.allow("forgot", "203.0.113.1", distinctive_email, now=0.0)
    for bucket in (limiter._recipient, limiter._recipient_ip):
        for key in bucket._buckets:
            assert distinctive_email not in key
            assert "do-not-leak-this-address" not in key


def test_existing_three_buckets_still_apply_unchanged() -> None:
    """Per-IP (unrelated recipients) and global caps are untouched by the fix."""
    limiter = EmailAbuseLimiter()
    now = 0.0
    # The per-IP cap (20/hour) still limits one IP across MANY different recipients.
    allowed = 0
    for i in range(25):
        if limiter.allow("register", "203.0.113.77", f"many-{i}@example.test", now=now):
            allowed += 1
    assert allowed == 20
