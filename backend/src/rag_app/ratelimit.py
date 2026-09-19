"""Token-bucket rate limiting.

The bucket logic is pure and deterministic (``consume`` takes ``now``) so it is easy to
unit-test. The in-memory ``RateLimiter`` is fine for a single instance; a multi-instance
deployment would back this with Redis. Cost-aware: a request can consume more than one
token (an expensive LLM call costs more), which defends against Denial-of-Wallet.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    def consume(self, amount: float, now: float) -> bool:
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimiter:
    """In-memory registry of token buckets keyed by an identity (IP or user id)."""

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=self.capacity,
                    refill_per_second=self.refill_per_second,
                    tokens=self.capacity,
                    updated_at=moment,
                )
                self._buckets[key] = bucket
            return bucket.consume(cost, moment)
