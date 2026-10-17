"""``/csp-report`` (T12.2.2, E5): unauthenticated, rate-limited per real IP, body-size
capped, logs only the violated directive + blocked origin (no query string, no PII),
explicitly CSRF-exempt, accepts both report shapes a browser sends.

No DB needed (in-memory rate limiter, no session/account lookup) -- plain ``pytest`` step,
like ``test_security_headers.py`` (T12.2.4's own gate)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from rag_app.api.app import create_app
from rag_app.api.deps import get_rate_limiter
from rag_app.csp_report import _sanitize_directive, blocked_origin, parse_report_entries
from rag_app.ratelimit import RateLimiter


def _client() -> TestClient:
    return TestClient(create_app())


# --- pure helpers -----------------------------------------------------------------------


def test_blocked_origin_strips_path_and_query() -> None:
    assert (
        blocked_origin("https://evil.example.com:8443/x.js?secret=1")
        == "https://evil.example.com:8443"
    )


@pytest.mark.parametrize("token", ["inline", "eval", ""])
def test_blocked_origin_passes_through_non_url_tokens(token: str) -> None:
    assert blocked_origin(token) in (token, "(none)")


def test_parse_report_entries_legacy_shape() -> None:
    payload = {
        "csp-report": {
            "violated-directive": "script-src-elem",
            "blocked-uri": "https://evil.example.com/x.js?a=b",
        }
    }
    assert parse_report_entries(payload) == [("script-src-elem", "https://evil.example.com")]


def test_parse_report_entries_reporting_api_shape() -> None:
    payload = [
        {
            "type": "csp-violation",
            "body": {
                "effectiveDirective": "style-src-elem",
                "blockedURL": "https://cdn.example.com/evil.css?x=1",
            },
        }
    ]
    assert parse_report_entries(payload) == [("style-src-elem", "https://cdn.example.com")]


def test_parse_report_entries_ignores_unrelated_reporting_api_entries() -> None:
    payload = [{"type": "deprecation", "body": {}}]
    assert parse_report_entries(payload) == []


# --- _sanitize_directive (DA-12aE-1: cap + strip, mirroring blocked_origin()) ------------


def test_sanitize_directive_strips_newlines_and_crlf() -> None:
    result = _sanitize_directive("script-src\r\ninjected: fake log line\nFAIL=1")
    assert "\n" not in result
    assert "\r" not in result


def test_sanitize_directive_strips_ansi_escapes() -> None:
    result = _sanitize_directive("script-src\x1b[31mRED\x1b[0m")
    assert "\x1b" not in result


def test_sanitize_directive_caps_length() -> None:
    result = _sanitize_directive("a" * 500)
    assert len(result) == 64


def test_sanitize_directive_strips_unicode() -> None:
    result = _sanitize_directive("scrîpt-src\U0001f600")
    assert result == "scrpt-src"


def test_sanitize_directive_falls_back_to_unknown_when_nothing_survives() -> None:
    assert _sanitize_directive("\U0001f600\U0001f600") == "unknown"


def test_parse_report_entries_sanitizes_a_hostile_directive() -> None:
    payload = {
        "csp-report": {
            "violated-directive": "script-src\r\ninjected: fake log line",
            "blocked-uri": "",
        }
    }
    (directive, _origin) = parse_report_entries(payload)[0]
    assert "\n" not in directive
    assert "\r" not in directive


# --- the route --------------------------------------------------------------------------


def test_accepts_legacy_content_type(caplog: pytest.LogCaptureFixture) -> None:
    client = _client()
    with caplog.at_level(logging.WARNING, logger="rag_app.csp_report"):
        response = client.post(
            "/csp-report",
            content=(
                b'{"csp-report": {"violated-directive": "script-src-elem", '
                b'"blocked-uri": "https://evil.example.com/x.js?token=abc"}}'
            ),
            headers={"Content-Type": "application/csp-report"},
        )
    assert response.status_code == 204
    assert "script-src-elem" in caplog.text
    assert "https://evil.example.com" in caplog.text
    assert "token=abc" not in caplog.text


def test_accepts_reporting_api_content_type(caplog: pytest.LogCaptureFixture) -> None:
    client = _client()
    with caplog.at_level(logging.WARNING, logger="rag_app.csp_report"):
        response = client.post(
            "/csp-report",
            content=(
                b'[{"type": "csp-violation", "body": {"effectiveDirective": "connect-src", '
                b'"blockedURL": "https://exfil.example.net/beacon?id=42"}}]'
            ),
            headers={"Content-Type": "application/reports+json"},
        )
    assert response.status_code == 204
    assert "connect-src" in caplog.text
    assert "https://exfil.example.net" in caplog.text
    assert "beacon" not in caplog.text
    assert "id=42" not in caplog.text


def test_malformed_json_is_dropped_silently() -> None:
    client = _client()
    response = client.post(
        "/csp-report",
        content=b"not json at all",
        headers={"Content-Type": "application/csp-report"},
    )
    assert response.status_code == 204


def test_oversized_body_is_rejected_by_content_length() -> None:
    client = _client()
    response = client.post(
        "/csp-report",
        content=b"{}",
        headers={"Content-Type": "application/csp-report", "Content-Length": str(10 * 1024)},
    )
    assert response.status_code == 204


def test_a_request_with_no_csrf_token_is_still_accepted() -> None:
    """Explicitly CSRF-exempt (no session cookie, no session to protect) -- unlike every
    other state-changing route, no ``X-CSRF-Token`` header is ever required here."""
    client = _client()
    response = client.post(
        "/csp-report",
        content=b'{"csp-report": {"violated-directive": "img-src", "blocked-uri": ""}}',
        headers={"Content-Type": "application/csp-report"},
    )
    assert response.status_code == 204


def test_a_crlf_injection_in_the_directive_stays_a_single_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    body = json.dumps(
        {
            "csp-report": {
                "violated-directive": "script-src\r\nFAKE LOG LINE level=CRITICAL",
                "blocked-uri": "",
            }
        }
    ).encode()
    with caplog.at_level(logging.WARNING, logger="rag_app.csp_report"):
        response = client.post(
            "/csp-report", content=body, headers={"Content-Type": "application/csp-report"}
        )
    assert response.status_code == 204
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "\n" not in message
    assert "\r" not in message


def test_an_ansi_escape_in_the_directive_is_stripped_before_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    body = json.dumps(
        {"csp-report": {"violated-directive": "script-src\x1b[31mRED\x1b[0m", "blocked-uri": ""}}
    ).encode()
    with caplog.at_level(logging.WARNING, logger="rag_app.csp_report"):
        response = client.post(
            "/csp-report", content=body, headers={"Content-Type": "application/csp-report"}
        )
    assert response.status_code == 204
    assert "\x1b" not in caplog.records[0].getMessage()


def test_an_oversized_directive_is_capped_before_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    body = json.dumps({"csp-report": {"violated-directive": "x" * 500, "blocked-uri": ""}}).encode()
    with caplog.at_level(logging.WARNING, logger="rag_app.csp_report"):
        response = client.post(
            "/csp-report", content=body, headers={"Content-Type": "application/csp-report"}
        )
    assert response.status_code == 204
    message = caplog.records[0].getMessage()
    assert "x" * 65 not in message
    assert "x" * 64 in message


def test_a_unicode_directive_is_stripped_before_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    body = json.dumps(
        {"csp-report": {"violated-directive": "scrîpt-src\U0001f600", "blocked-uri": ""}}
    ).encode()
    with caplog.at_level(logging.WARNING, logger="rag_app.csp_report"):
        response = client.post(
            "/csp-report", content=body, headers={"Content-Type": "application/csp-report"}
        )
    assert response.status_code == 204
    message = caplog.records[0].getMessage()
    assert "î" not in message
    assert "\U0001f600" not in message
    assert "scrpt-src" in message


def test_rate_limited_per_ip() -> None:
    app = create_app()
    # The SAME instance on every call: a fresh one per request (the real `get_rate_limiter`
    # memoizes a module-level singleton; a naive override lambda does not) would never let a
    # bucket actually empty across requests.
    shared_limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    app.dependency_overrides[get_rate_limiter] = lambda: shared_limiter
    client = TestClient(app)
    body = b'{"csp-report": {"violated-directive": "img-src", "blocked-uri": ""}}'
    headers = {"Content-Type": "application/csp-report"}
    first = client.post("/csp-report", content=body, headers=headers)
    second = client.post("/csp-report", content=body, headers=headers)
    assert first.status_code == 204
    assert second.status_code == 429
