"""``GET /wake`` -- a light, unauthenticated wake ping (T13.13.2, DEBT-061).

Called once per page load by the frontend's ``/login`` and ``/chat`` pages (via the
BFF's own ``/api/wake``, ``frontend/app/api/wake/route.ts``), so a visitor landing on a
genuinely cold Azure stack nudges Ollama awake the moment the PAGE loads, in parallel
with whatever else is cold-starting, instead of only once they submit a question.

Unauthenticated by design (same reasoning as ``/csp-report``, ``rag_app.csp_report``: a
visitor reaches ``/login`` before any session exists) and deliberately minimal:

- no request body, no query string -- nothing for a caller to control beyond "ping";
- a fixed 204, always, regardless of whether a warm-up was actually started, skipped
  (one already in flight) or whatever the warm-up itself eventually does -- a caller
  learns NOTHING about the backend's internal state (no user/existence leak, no new
  attack surface beyond "this endpoint exists and answers 204");
- rate-limited per real IP (``rag_app.api.deps.rate_limit_wake``), the same shared
  token bucket every other unauthenticated route in this app uses;
- reuses ``rag_app.warmup``'s single-flight guard -- the SAME one the API lifespan's
  own start-up warm-up (T13.13.1) uses, so a wake ping arriving while that is still
  running (or while another wake ping's warm-up is still running) is a no-op, not a
  second concurrent embed call.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status

from rag_app import warmup
from rag_app.api.deps import rate_limit_wake

logger = logging.getLogger("rag_app.wake")

router = APIRouter()

_NO_CONTENT = Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/wake",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
    tags=["stack"],
    dependencies=[Depends(rate_limit_wake)],
)
def wake() -> Response:
    # Defense in depth: ``trigger_warm_up`` already guarantees it never raises (its own
    # contract, ``rag_app.warmup``), but this route must stay a plain 204 even if that
    # contract were ever violated by a future change there -- an exception here must never
    # become a 500 that would hint at backend internals to an unauthenticated caller.
    try:
        warmup.trigger_warm_up()
    except Exception:  # noqa: BLE001 - see above; always logged, never surfaced
        logger.warning("wake ping: trigger_warm_up raised unexpectedly", exc_info=True)
    return _NO_CONTENT
