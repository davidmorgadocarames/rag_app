"""CSRF protection (T12.1.4, P12-D2, DEC-010 = A).

A double-submit token bound to the session: ``csrf_token`` is
``HMAC-SHA256(CSRF_SECRET, raw_session_id)``, hex-encoded. It is issued (and rotated) in a
readable (non-``HttpOnly``) ``__Host-`` cookie whenever a session is created, so client-side
JavaScript can read it and echo it back in the ``X-CSRF-Token`` header -- the session cookie
itself stays ``HttpOnly`` and is never readable by page script. The BFF forwards both the
``Origin`` header and ``X-CSRF-Token`` unchanged (12a-C, T12.1.5).

``require_csrf`` is the dependency every cookie-authenticated state-changing route (POST /
PATCH / PUT / DELETE) adds: a request with NO session cookie at all is the legacy Bearer-only
rollback path (12a only, TF10) and is exempt -- a Bearer token is never sent automatically by
the browser, so it cannot be forged cross-site. A request that DOES carry the session cookie
must also carry a matching ``X-CSRF-Token`` (constant-time compare) and, when an ``Origin``
header is present, it must match ``FRONTEND_ORIGIN``.

``require_origin`` is the lighter check for register/login, where no session exists yet: a
present-but-wrong ``Origin`` is rejected; a missing one is allowed (curl/legacy clients, and
browsers that omit ``Origin`` on some same-origin requests)."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status

from rag_app import sessions as session_service
from rag_app.config import get_settings

CSRF_COOKIE_NAME = "__Host-secrag_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def csrf_token(secret: str, raw_session_id: str) -> str:
    """HMAC-SHA256(secret, raw session id), hex-encoded -- bound to this one session."""
    return hmac.new(secret.encode(), raw_session_id.encode(), hashlib.sha256).hexdigest()


def _origin_ok(request: Request, frontend_origin: str) -> bool:
    origin = request.headers.get("origin")
    return origin is None or origin == frontend_origin


def require_origin(request: Request) -> None:
    """Login/register: no session cookie exists yet, so the only proof available is the
    browser-set ``Origin`` header. Rejects a present-but-wrong one; a missing one is allowed."""
    if not _origin_ok(request, get_settings().frontend_origin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "origin not allowed")


def require_csrf(request: Request) -> None:
    """Every cookie-authenticated state-changing route. Exempt when no session cookie is
    present at all (the legacy Bearer-only rollback path, 12a, TF10)."""
    raw_session = request.cookies.get(session_service.SESSION_COOKIE_NAME)
    if raw_session is None:
        return
    settings = get_settings()
    if not _origin_ok(request, settings.frontend_origin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "origin not allowed")
    provided = request.headers.get(CSRF_HEADER_NAME, "")
    expected = csrf_token(settings.csrf_secret, raw_session)
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")
