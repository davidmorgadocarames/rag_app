"""Email abuse limits (T12.4.8): bounds how much email ``register`` / ``resend-verification``
/ ``forgot-password`` can make this backend fan out -- per real IP (one attacker cannot make
the backend spam many addresses), per recipient (one attacker cannot flood ONE victim's
inbox, e.g. from many source IPs or via repeated clicks) and a small global cap (defence in
depth against exhausting the SMTP/ACS quota outright, whatever the per-IP/per-recipient
limits allow).

**X10 (no TOCTOU).** Every check is ``RateLimiter.allow`` (``rag_app.ratelimit``), which
takes the bucket's own lock and checks-and-decrements the token count in ONE call -- never a
separate "read the counter" followed by a "decrement it" (the same reasoning as
``rag_app.lockout``'s atomic upsert, just in-memory instead of in the database).

**Code constants (same reasoning as ``rag_app.sessions``' timeouts, B6): a security policy,
not an Azure configuration value** -- no new secret/env var for this. In-memory (like
``rag_app.risk.SignupTracker``/the chat ``RateLimiter``): fine for a single instance; a
multi-instance deployment would back this with Redis.
"""

from __future__ import annotations

from rag_app.ratelimit import RateLimiter

# (capacity, refill_per_second): `capacity` requests let through immediately, then refills
# at the steady-state rate below. Deliberately small -- this is a portfolio-scale app, not a
# mail relay.
_IP_CAPACITY = 20.0
_IP_REFILL_PER_SECOND = _IP_CAPACITY / 3600  # 20 per hour per real IP
_RECIPIENT_CAPACITY = 5.0
_RECIPIENT_REFILL_PER_SECOND = _RECIPIENT_CAPACITY / 3600  # 5 per hour per recipient address
_GLOBAL_CAPACITY = 100.0
_GLOBAL_REFILL_PER_SECOND = _GLOBAL_CAPACITY / 3600  # 100 per hour, per action, whole backend


class EmailAbuseLimiter:
    """Three independent token buckets per ``action`` ("register" / "resend" / "forgot"):
    real IP, recipient address and a global cap. A fresh instance (as tests use, overriding
    ``rag_app.api.deps.get_email_abuse_limiter``) never carries state from an unrelated
    test/request -- the real app uses ONE process-wide instance, like the chat
    ``RateLimiter``."""

    def __init__(self) -> None:
        self._ip = RateLimiter(_IP_CAPACITY, _IP_REFILL_PER_SECOND)
        self._recipient = RateLimiter(_RECIPIENT_CAPACITY, _RECIPIENT_REFILL_PER_SECOND)
        self._global = RateLimiter(_GLOBAL_CAPACITY, _GLOBAL_REFILL_PER_SECOND)

    def allow(self, action: str, ip: str, email: str) -> bool:
        """True iff the IP, recipient AND global buckets for ``action`` all still have a
        token. Short-circuits (``and``) on the first bucket that is empty -- a caller
        rejected by, say, the IP bucket never also spends a recipient/global token it did
        not need to; the common (allowed) case still spends one token in all three."""
        normalised_email = email.strip().lower()
        return (
            self._ip.allow(f"{action}:ip:{ip}")
            and self._recipient.allow(f"{action}:to:{normalised_email}")
            and self._global.allow(f"{action}:global")
        )
