"""``/csp-report`` — the backend endpoint the frontend's CSP (nonce + ``report-uri``/
``report-to``, ``frontend/lib/csp.ts``) points browsers at (T12.2.2, E5).

Unauthenticated by design (a violation can fire before any session exists, e.g. on
``/login``) and explicitly **CSRF-exempt**: there is no state to protect (this route never
reads or writes a session/account) and no ``require_csrf``/``require_origin`` dependency is
attached to it, unlike every other state-changing route in ``api/auth.py``/``api/
conversations.py`` -- a cross-site form cannot "forge" a report that is itself the
information this endpoint exists to collect.
``test_csp_report.py::test_a_request_with_no_csrf_token_is_still_accepted`` pins this.

Accepts both report shapes a browser may send:
- the legacy ``application/csp-report`` body, ``{"csp-report": {"violated-directive": ...,
  "blocked-uri": ...}}`` (still what Firefox/Safari send for ``report-uri``);
- the newer Reporting API's ``application/reports+json`` body, a JSON **array** of
  ``{"type": "csp-violation", "body": {"effectiveDirective": ..., "blockedURL": ...}}``
  (what Chromium sends for ``report-to``; a browser may batch several reports in one POST).

Logs only the **violated directive** and the **blocked origin** (scheme + host[:port], never
the path or query string -- a ``blocked-uri`` can legitimately carry a query string, and
nothing here is allowed to echo one into a log line, T11.2.15/R6-5 pattern). Never raises on
a malformed, oversized or wrongly-typed body: a hostile or buggy report is dropped silently,
not a 500 or a 400 that could itself become a probe surface.

Both the directive and the origin are sanitised before logging (``_sanitize_directive()``/
``blocked_origin()``): this route is unauthenticated by design, so a hostile POST fully
controls both fields, and a naive ``logger.warning("...%s...", directive, ...)`` would let it
inject newlines/CRLF (forged multi-line log entries), ANSI escapes (log-viewer/terminal
abuse) or an oversized value into the log stream (DA-12aE-1).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response, status

from rag_app.api.deps import rate_limit_csp_report

logger = logging.getLogger("rag_app.csp_report")

router = APIRouter()

# CSP reports are small JSON blobs (a handful of short fields); this is generous headroom
# over any real one while still bounding the memory/log-line cost of a hostile body.
MAX_BODY_BYTES = 8 * 1024
_NO_CONTENT = Response(status_code=status.HTTP_204_NO_CONTENT)

# Real CSP directive names (``script-src-elem``, ``style-src``, ``report-to``, ...) are all
# short tokens of ASCII letters, digits and hyphens. The directive field is attacker-
# controlled (this route is unauthenticated by design), so anything outside that charset --
# newlines/CRLF, ANSI escapes, other control characters, unicode -- is stripped before the
# value ever reaches ``logger.warning``, the same log-injection concern (forged multi-line
# log entries, terminal/log-viewer escape-sequence abuse) that ``blocked_origin()`` already
# guards against for the URL field (T11.2.15/R6-5 pattern, DA-12aE-1).
_DIRECTIVE_MAX_LEN = 64
_DIRECTIVE_DISALLOWED = re.compile(r"[^A-Za-z0-9-]")


def _sanitize_directive(directive: str) -> str:
    """Cap to ``_DIRECTIVE_MAX_LEN`` chars and strip anything outside the CSP directive-name
    charset (ASCII letters/digits/hyphen). Never returns an empty string."""
    cleaned = _DIRECTIVE_DISALLOWED.sub("", directive)[:_DIRECTIVE_MAX_LEN]
    return cleaned or "unknown"


def blocked_origin(uri: str) -> str:
    """``scheme://host[:port]`` only -- never the path or query (no PII, no full URLs).

    ``uri`` may be a non-URL token the spec allows (``"inline"``, ``"eval"``, ``""``): those
    are returned verbatim (capped) rather than guessed at.
    """
    if not uri:
        return "(none)"
    parts = urlsplit(uri)
    if not parts.scheme or not parts.netloc:
        return uri[:32]
    return f"{parts.scheme}://{parts.netloc}"


def _legacy_entries(payload: object) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    report = payload.get("csp-report")
    if not isinstance(report, dict):
        return []
    directive = str(
        report.get("violated-directive") or report.get("effective-directive") or "unknown"
    )
    return [(_sanitize_directive(directive), blocked_origin(str(report.get("blocked-uri") or "")))]


def _reporting_api_entries(payload: object) -> list[tuple[str, str]]:
    if not isinstance(payload, list):
        return []
    entries: list[tuple[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "csp-violation":
            continue
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        directive = str(body.get("effectiveDirective") or "unknown")
        entries.append(
            (_sanitize_directive(directive), blocked_origin(str(body.get("blockedURL") or "")))
        )
    return entries


def parse_report_entries(payload: object) -> list[tuple[str, str]]:
    """(directive, blocked_origin) pairs from either shape; unrecognised payloads -> []."""
    if isinstance(payload, list):
        return _reporting_api_entries(payload)
    return _legacy_entries(payload)


@router.post(
    "/csp-report",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
    dependencies=[Depends(rate_limit_csp_report)],
)
async def csp_report(request: Request) -> Response:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return _NO_CONTENT
        except ValueError:
            return _NO_CONTENT
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _NO_CONTENT
    try:
        payload = json.loads(raw)
    except ValueError:
        return _NO_CONTENT

    for directive, origin in parse_report_entries(payload):
        logger.warning("csp violation: directive=%s blocked_origin=%s", directive, origin)
    return _NO_CONTENT
