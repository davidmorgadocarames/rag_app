"""``GET /wake`` -- a light, unauthenticated wake ping (T13.13.2, DEBT-061; DEC-050/
T13.J4-fix narrowed its scope -- see below).

Called once per page load by the frontend's ``/login`` and ``/chat`` pages (via the
BFF's own ``/api/wake``, ``frontend/app/api/wake/route.ts``), so a visitor landing on a
genuinely cold Azure stack nudges the BACKEND awake the moment the PAGE loads, in
parallel with whatever else is cold-starting, instead of only once they submit a
question.

**DEC-050 (DA-13-J3J4-5, user 2026-11-23): this route no longer triggers the Ollama
warm-up.** The original design re-ran ``rag_app.warmup.trigger_warm_up()`` on every call,
reusing the generic shared rate limiter (60 capacity / 1 req/s refill) -- a single
unauthenticated actor sustaining ~1 req/s could keep re-triggering it forever, which wakes
the SEPARATE, also-scaled-to-zero Ollama Container App and keeps it running past its own
idle cooldown, defeating scale-to-zero at a real € cost with no account/CAPTCHA needed.
``GET /wake`` now does exactly one thing -- nudge THIS backend process awake (reaching it
at all already proves the container is up, nothing else is needed) -- and carries its own
much tighter rate limit (``rag_app.api.deps.rate_limit_wake_narrow``, 1 request per 60s per
key, never the generic 60/1-per-second bucket). The Ollama warm-up itself now fires from
exactly two AUTHENTICATED-or-trusted call sites instead (``rag_app.warmup`` docstring):
the API lifespan at start-up (T13.13.1, unchanged) and, server-side, after a SUCCESSFUL
login (``rag_app.api.auth.login``) or when an authenticated session loads ``/chat``
(``rag_app.api.conversations.list_conversations``) -- never from an unauthenticated
request.

Still deliberately minimal:

- no request body, no query string -- nothing for a caller to control beyond "ping";
- a fixed 204, always -- a caller learns NOTHING about the backend's internal state
  (no user/existence leak, no new attack surface beyond "this endpoint exists and
  answers 204");
- rate-limited per real IP (``rag_app.api.deps.rate_limit_wake_narrow``), its own bucket,
  not shared with any other route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from rag_app.api.deps import rate_limit_wake_narrow

router = APIRouter()

_NO_CONTENT = Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/wake",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
    tags=["stack"],
    dependencies=[Depends(rate_limit_wake_narrow)],
)
def wake() -> Response:
    # DEC-050: deliberately a no-op beyond the 204 itself -- reaching this handler at all
    # already proves the backend process is up and serving, which is the only thing this
    # route exists to prove. It must NEVER trigger the Ollama warm-up (see module
    # docstring) -- that happens only after a successful login or an authenticated /chat
    # page load.
    return _NO_CONTENT
