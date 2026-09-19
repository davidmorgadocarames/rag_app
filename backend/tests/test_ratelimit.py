"""Unit tests for the token-bucket rate limiter (deterministic via explicit now)."""

from __future__ import annotations

from rag_app.ratelimit import RateLimiter, TokenBucket


def test_bucket_consume_then_exhaust() -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1, tokens=10, updated_at=0.0)
    assert bucket.consume(6, now=0.0) is True  # 10 -> 4
    assert bucket.consume(6, now=0.0) is False  # only 4 left


def test_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1, tokens=0, updated_at=0.0)
    assert bucket.consume(5, now=3.0) is False  # only 3 refilled
    assert bucket.consume(5, now=5.0) is True  # 5 refilled


def test_bucket_caps_at_capacity() -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1, tokens=0, updated_at=0.0)
    assert bucket.consume(10, now=1000.0) is True  # refill capped at capacity


def test_rate_limiter_is_per_key() -> None:
    limiter = RateLimiter(capacity=2, refill_per_second=0)
    assert limiter.allow("a", 1, now=0) is True
    assert limiter.allow("a", 1, now=0) is True
    assert limiter.allow("a", 1, now=0) is False  # key "a" exhausted
    assert limiter.allow("b", 1, now=0) is True  # separate key unaffected
