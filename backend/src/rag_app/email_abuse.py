"""Email abuse limits (T12.4.8): bounds how much email ``register`` / ``resend-verification``
/ ``forgot-password`` can make this backend fan out -- per real IP (one attacker cannot make
the backend spam many addresses), per recipient (one attacker cannot flood ONE victim's
inbox, e.g. from many source IPs or via repeated clicks) and a small global cap (defence in
depth against exhausting the SMTP/ACS quota outright, whatever the per-IP/per-recipient
limits allow).

**Fix block (DA-12bC-1 / DEC-026 = A, 2026-11-12).** The original design had only ONE
recipient-wide bucket, shared across every source IP: a single attacker IP trickling
requests at just under the bucket's refill rate could keep a victim's address permanently
drained (well under the per-IP/global caps, so neither ever engaged), silently suppressing
the victim's own password-reset/verification emails indefinitely. A fourth bucket now keys
on ``(recipient, requester IP)`` (``_RECIPIENT_IP_CAPACITY`` / hour) so a single IP's share
of one recipient's traffic is capped small; the pure-recipient bucket is the backstop against
MANY-IP floods and is deliberately **larger** than what any one IP can draw through its own
``(recipient, IP)`` bucket (``_RECIPIENT_CAPACITY`` > ``_RECIPIENT_IP_CAPACITY``, see the
constants below for the exact numbers and the arithmetic) -- a lone attacker IP can never
starve the backstop for everyone else. See ``docs/security/THREAT_MODEL.md`` (the design note
right before REC-07) for the full reasoning, the companion regression tests, and the residual
many-IP risk this change explicitly accepts -- as few as ~7 distinct source IPs (ceil(20/3))
trickling in parallel can still exhaust the pure-recipient backstop -- the same way T12.4.2's
login-lockout design note (DEBT-034) accepts the equivalent gap for that feature.

**X10 (no TOCTOU).** Every check is ``RateLimiter.allow`` (``rag_app.ratelimit``), which
takes the bucket's own lock and checks-and-decrements the token count in ONE call -- never a
separate "read the counter" followed by a "decrement it" (the same reasoning as
``rag_app.lockout``'s atomic upsert, just in-memory instead of in the database).

**Code constants (same reasoning as ``rag_app.sessions``' timeouts, B6): a security policy,
not an Azure configuration value** -- no new secret/env var for this. The recipient identity
itself is hashed (HMAC-SHA256) with a random salt generated once per process at ``__init__``
-- never configured, never logged, never persisted -- so the raw email never appears in an
in-memory bucket key; there is nothing to keep consistent across restarts because every
bucket here is gone on restart anyway.

**Single-replica limitation (DA-12bC-3).** Like every other in-memory limiter in this family
(``rag_app.ratelimit.RateLimiter``, the chat rate limiter; ``rag_app.risk.SignupTracker``),
this state lives in ONE process's memory: correct only while the backend runs a single
replica, exactly the constraint RISK-03/DEBT-010 (``docs/security/THREAT_MODEL.md`` §5.4)
already documents for the reranker concurrency cap. Multi-replica support (16.3, KEDA
autoscaling) must back this whole family with a shared store (e.g. Redis) before the backend
can run more than one replica -- this is not a new gap, just the same one restated for a
second subsystem.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from rag_app.ratelimit import RateLimiter

# (capacity, refill_per_second): `capacity` requests let through immediately, then refills
# at the steady-state rate below. Deliberately small -- this is a portfolio-scale app, not a
# mail relay.
_IP_CAPACITY = 20.0
_IP_REFILL_PER_SECOND = _IP_CAPACITY / 3600  # 20 per hour per real IP

# New in the DA-12bC-1/DEC-026 fix: one source IP's own share of ONE recipient's traffic.
# Small on purpose -- a genuine user retrying forgot-password a couple of times from the
# same network in an hour is still well under this; a trickle attacker is capped here no
# matter how patient they are.
_RECIPIENT_IP_CAPACITY = 3.0
_RECIPIENT_IP_REFILL_PER_SECOND = _RECIPIENT_IP_CAPACITY / 3600  # 3 per hour per (to, ip)

# The pure-recipient bucket: the backstop against a MANY-IP flood of one address (defence in
# depth kept from the original design), raised from the original 5/hour so it is clearly
# LARGER than what a single IP can ever draw through `_RECIPIENT_IP_CAPACITY` above (20 > 3,
# with the backstop's refill rate -- 20/hour -- also well above the 3/hour ceiling a lone
# attacker IP is held to, so the backstop never runs dry from one source alone; see the
# THREAT_MODEL design note for the arithmetic and the residual many-IP risk this accepts).
_RECIPIENT_CAPACITY = 20.0
_RECIPIENT_REFILL_PER_SECOND = _RECIPIENT_CAPACITY / 3600  # 20 per hour per recipient address

_GLOBAL_CAPACITY = 100.0
_GLOBAL_REFILL_PER_SECOND = _GLOBAL_CAPACITY / 3600  # 100 per hour, per action, whole backend


class EmailAbuseLimiter:
    """Four independent token buckets per ``action`` ("register" / "resend" / "forgot"):
    real IP, ``(recipient, real IP)``, pure recipient (backstop) and a global cap. A fresh
    instance (as tests use, overriding ``rag_app.api.deps.get_email_abuse_limiter``) never
    carries state from an unrelated test/request -- the real app uses ONE process-wide
    instance, like the chat ``RateLimiter``."""

    def __init__(self) -> None:
        self._ip = RateLimiter(_IP_CAPACITY, _IP_REFILL_PER_SECOND)
        self._recipient_ip = RateLimiter(_RECIPIENT_IP_CAPACITY, _RECIPIENT_IP_REFILL_PER_SECOND)
        self._recipient = RateLimiter(_RECIPIENT_CAPACITY, _RECIPIENT_REFILL_PER_SECOND)
        self._global = RateLimiter(_GLOBAL_CAPACITY, _GLOBAL_REFILL_PER_SECOND)
        # A random per-process salt -- never configured, never logged, never persisted. Only
        # needed so the SAME recipient maps to the SAME bucket key for the lifetime of this
        # one process; it does not need to survive a restart (the buckets themselves don't).
        self._recipient_salt = secrets.token_bytes(32)

    def _recipient_digest(self, email: str) -> str:
        """``HMAC-SHA256(per-process salt, lower(email))``, hex-encoded -- never the raw
        email itself, so it never appears in an in-memory bucket key or (were one ever added)
        a log line built from one."""
        normalised = email.strip().lower()
        return hmac.new(self._recipient_salt, normalised.encode(), hashlib.sha256).hexdigest()

    def allow(self, action: str, ip: str, email: str, *, now: float | None = None) -> bool:
        """True iff the IP, ``(recipient, IP)``, recipient AND global buckets for ``action``
        all still have a token. Short-circuits (``and``) on the first bucket that is empty --
        a caller rejected by an earlier bucket never also spends a token in a later one it did
        not need to; the common (allowed) case still spends one token in all four. ``now`` is
        only ever overridden by tests (deterministic, no real sleeping)."""
        digest = self._recipient_digest(email)
        return (
            self._ip.allow(f"{action}:ip:{ip}", now=now)
            and self._recipient_ip.allow(f"{action}:to-ip:{digest}:{ip}", now=now)
            and self._recipient.allow(f"{action}:to:{digest}", now=now)
            and self._global.allow(f"{action}:global", now=now)
        )
