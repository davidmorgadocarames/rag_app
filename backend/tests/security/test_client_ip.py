"""Signed client IP (T12.1.6, T5/V7): the BFF signs the real client address it derived from
``X-Forwarded-For``; the backend trusts only a fresh, correctly-signed
``X-SecRAG-Client-IP`` header and otherwise falls back to the raw connection address and
records a security event. Rate limiting is keyed by account/user + the resolved IP, so one
shared Azure-internal address can no longer bucket every caller together once the signature
is adopted.

Marked ``db`` (like every file in this package, T12.1.0): these specific tests build a
``Request`` directly and never touch the database, but staying inside
``backend/tests/security/`` keeps them out of the plain ``pytest`` step (``-m "not db"``)
and inside ``security-suite`` only -- the existing convention that stops a test running
twice across gate modes (see ``conftest.py``)."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rag_app import metrics
from rag_app.api.deps import _client_ip, rate_limit_chat, rate_limit_login
from rag_app.client_ip import CLIENT_IP_HEADER, sign_client_ip, verify_client_ip_header
from rag_app.config import get_settings
from rag_app.ratelimit import RateLimiter

pytestmark = [pytest.mark.db, pytest.mark.security]

# Low-entropy, repeated-character throwaway values (not real secrets) -- a readable,
# "current-key-..." style literal here trips gitleaks' generic-api-key rule in git history.
KEY = "k" * 36
PREVIOUS_KEY = "p" * 36


def _request(
    headers: dict[str, str], *, method: str = "POST", path: str = "/chat/stream"
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("10.0.0.5", 51234),  # the shared Azure-internal address, if ever reached
    }
    return Request(scope)


# --- rag_app.client_ip (pure sign/verify) --------------------------------------------------


def test_the_wire_format_matches_the_frontend_signer() -> None:
    """Cross-language fixed vector (frontend/lib/clientIp.test.ts): the real signer is the
    BFF (TypeScript); this proves both sides build the EXACT same header for the same inputs,
    so the format cannot silently drift between the two implementations."""
    header = sign_client_ip("shared-test-key-32-chars-long-00", "203.0.113.99", "POST",
                             "/chat/stream", now=1_700_000_000)  # fmt: skip
    assert header == (
        "203.0.113.99|1700000000|"
        "dd60e23e6ade9717baed6319761684493fbc7f4e32d38d08349cf89dd958e43a"
    )


def test_a_freshly_signed_header_verifies() -> None:
    header = sign_client_ip(KEY, "203.0.113.7", "POST", "/chat/stream", now=1_000)
    resolved = verify_client_ip_header(header, [KEY], "POST", "/chat/stream", now=1_010)
    assert resolved == "203.0.113.7"


def test_a_stale_signature_outside_the_30s_window_is_rejected() -> None:
    header = sign_client_ip(KEY, "203.0.113.7", "POST", "/chat/stream", now=1_000)
    assert verify_client_ip_header(header, [KEY], "POST", "/chat/stream", now=1_031) is None


def test_signed_for_a_different_method_or_path_does_not_verify() -> None:
    header = sign_client_ip(KEY, "203.0.113.7", "POST", "/chat/stream", now=1_000)
    assert verify_client_ip_header(header, [KEY], "DELETE", "/chat/stream", now=1_000) is None
    assert verify_client_ip_header(header, [KEY], "POST", "/account", now=1_000) is None


def test_an_unknown_key_does_not_verify() -> None:
    header = sign_client_ip(KEY, "203.0.113.7", "POST", "/chat/stream", now=1_000)
    assert verify_client_ip_header(header, ["some-other-key-entirely-0000000000"], "POST",
                                    "/chat/stream", now=1_000) is None  # fmt: skip


def test_the_previous_key_still_verifies_during_a_rotation() -> None:
    header = sign_client_ip(PREVIOUS_KEY, "203.0.113.7", "POST", "/chat/stream", now=1_000)
    assert (
        verify_client_ip_header(header, [KEY, PREVIOUS_KEY], "POST", "/chat/stream", now=1_000)
        == "203.0.113.7"
    )


def test_a_malformed_header_does_not_verify() -> None:
    assert verify_client_ip_header("not-the-right-shape", [KEY], "POST", "/chat/stream") is None


# --- rag_app.api.deps._client_ip (missing/forged/stale -> fallback + event) ----------------


@pytest.fixture()
def client_ip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIENT_IP_HMAC_KEYS", KEY)


def test_a_valid_signature_is_trusted_over_the_connection_address(client_ip_env: None) -> None:
    header = sign_client_ip(KEY, "198.51.100.9", "POST", "/chat/stream")
    assert _client_ip(_request({CLIENT_IP_HEADER: header})) == "198.51.100.9"


_STALE_HEADER = sign_client_ip(KEY, "198.51.100.9", "POST", "/chat/stream", now=1)


@pytest.mark.parametrize(
    "headers",
    [
        {},  # missing
        {CLIENT_IP_HEADER: "198.51.100.9|9999999999|deadbeef"},  # forged (wrong signature)
        {CLIENT_IP_HEADER: _STALE_HEADER},  # stale: signed, but far outside the 30s window
    ],
    ids=["missing", "forged", "stale"],
)
def test_missing_forged_or_stale_signature_falls_back_and_records_an_event(
    client_ip_env: None, headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    before = metrics.CLIENT_IP_SIGNATURE_EVENTS._value.get()
    with caplog.at_level(logging.WARNING, logger="rag_app.api.deps"):
        resolved = _client_ip(_request(headers))
    assert resolved == "10.0.0.5"  # the connection address, never the (possibly forged) header
    after = metrics.CLIENT_IP_SIGNATURE_EVENTS._value.get()
    assert after == before + 1
    assert any("client IP" in r.message for r in caplog.records)
    # Log hygiene: the event line never carries the candidate/forged IP value itself.
    assert not any("198.51.100.9" in r.message for r in caplog.records)


def test_an_incoming_header_is_simply_ignored_when_the_feature_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIENT_IP_HMAC_KEYS", "")
    header = sign_client_ip(KEY, "198.51.100.9", "POST", "/chat/stream")
    # No key configured at all: today's normal path (12a-C/D, before T12.1.7) -- the header,
    # even a validly-signed one, is beside the point, and this must not look like an attack.
    assert _client_ip(_request({CLIENT_IP_HEADER: header})) == "10.0.0.5"


# --- rate limiting keyed by account/user + real IP -----------------------------------------


def test_rate_limit_chat_buckets_two_signed_clients_separately(client_ip_env: None) -> None:
    """Done-when (T12.1.6): two simulated clients -> two buckets. Both requests carry the
    SAME user id (``request.state.user_id``, set by ``get_current_user``) but a DIFFERENT
    signed real IP, so only the IP half of the key can explain the separation."""
    cost = get_settings().rate_limit_chat_cost
    limiter = RateLimiter(capacity=cost, refill_per_second=0.0)
    user_id = "11111111-1111-1111-1111-111111111111"

    def req(ip: str) -> Request:
        request = _request({CLIENT_IP_HEADER: sign_client_ip(KEY, ip, "POST", "/chat/stream")})
        request.state.user_id = user_id
        return request

    rate_limit_chat(req("203.0.113.1"), limiter)  # exhausts client A's bucket
    with pytest.raises(HTTPException):
        rate_limit_chat(req("203.0.113.1"), limiter)  # same client again: refused
    rate_limit_chat(req("203.0.113.2"), limiter)  # a DIFFERENT real IP: its own, still-full bucket


def test_rate_limit_login_is_keyed_by_account_not_only_ip() -> None:
    """The submitted email is read from the raw body (not a second typed body parameter --
    see ``rate_limit_login``'s docstring) so two different accounts from the SAME connection
    address get separate buckets."""
    limiter = RateLimiter(capacity=1.0, refill_per_second=0.0)

    def make_request(email: str) -> Request:
        async def receive() -> dict[str, object]:
            body = json.dumps({"email": email, "password": "x"}).encode()
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [(b"content-type", b"application/json")],
            "client": ("10.0.0.5", 51234),
        }
        return Request(scope, receive)

    asyncio.run(rate_limit_login(make_request("alice@example.test"), limiter))  # exhausts alice
    with pytest.raises(HTTPException):
        asyncio.run(rate_limit_login(make_request("alice@example.test"), limiter))
    asyncio.run(rate_limit_login(make_request("bob@example.test"), limiter))  # a different account
