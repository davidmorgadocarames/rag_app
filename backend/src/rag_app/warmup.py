"""Background warm-up of the Ollama embedder (T13.13.1/T13.13.2, DEBT-061).

On an Azure cold start the path is serial: backend container start (torch import +
reranker load/warm-up in the API lifespan, ``rag_app.reranking.CrossEncoderReranker
.warm_up``) -> classify -> retrieving -> only THEN ``retrieval.embed_query()`` calls
Ollama, a SEPARATE Container App also scaled to zero. Ollama waking ``bge-m3`` is the
dominant cost (~19.7s avg on Azure, 13-J3b finding) and today it only starts once the
backend has already finished waking itself -- two cold starts stacked one after another
instead of overlapping.

:func:`trigger_warm_up` fires the SAME embed call Ollama would eventually receive from
a real question, in the background, as early as possible:

- the API lifespan calls it once, right after the reranker warm-up, before the app
  starts serving traffic (T13.13.1);
- a SUCCESSFUL ``POST /auth/login`` calls it again (``rag_app.api.auth.login``), in case
  Ollama went cold again since the backend itself woke up;
- an authenticated ``GET /conversations`` -- the call the frontend's `/chat` page already
  makes on every page load with a valid session -- calls it too
  (``rag_app.api.conversations.list_conversations``).

DEC-050 (T13.J4-fix, DA-13-J3J4-5, 2026-11-23): ``GET /wake`` (``rag_app.wake``) USED TO be
a third call site (T13.13.2) -- re-triggering this on every unauthenticated page load let a
single actor sustaining ~1 req/s keep Ollama awake forever against the generic shared rate
limiter, defeating scale-to-zero at a real € cost. `/wake` now only proves "the backend
process answered" and never calls this module at all; every remaining call site above is
either server-controlled (the lifespan) or requires a valid session (login success,
authenticated `/chat` load) -- never reachable from an unauthenticated request.

Every call site shares the SAME single-flight guard below, so a cold backend restarting
under load, or several authenticated page loads within the embed timeout window, never
pile up N concurrent warm-up calls against an already-waking Ollama -- a warm-up already in
flight is skipped, not queued (the one in flight warms it up for everyone regardless).

**Non-blocking, failure-tolerant.** :func:`trigger_warm_up` itself never blocks (it only
starts a background thread and returns) and never raises: the actual embed call runs on
its own daemon thread, and any exception there is only logged -- this must never delay
``/health``, never fail the API lifespan, and never surface to an unauthenticated
``/wake`` caller.

**Never pollutes the per-answer timing/metrics channels.** ``rag_app.timing``'s
stage/step recorder is a ``contextvars``-based context manager that is simply absent on
a freshly-started OS thread (Python threads do not inherit the parent's contextvars),
so even if this ran inside a real answer's recorder scope it could not attach to it --
unlike ``CrossEncoderReranker.warm_up`` (same process, same thread, deliberately wrapped
in ``timing.stage(...)`` purely for symmetry), this module does not bother wrapping the
call in a stage at all: there is never a recorder to find.
"""

from __future__ import annotations

import logging
import threading

from rag_app import metrics
from rag_app.embeddings import OllamaEmbedder

logger = logging.getLogger("rag_app.warmup")

_lock = threading.Lock()
_in_progress = False


def _run(embedder: OllamaEmbedder) -> None:
    global _in_progress
    try:
        embedder.embed_one("warm-up")
        metrics.OLLAMA_WARMUP_TOTAL.labels(result="ok").inc()
    except Exception:  # noqa: BLE001 - best-effort warm-up, must never propagate
        logger.warning("ollama warm-up embed failed", exc_info=True)
        metrics.OLLAMA_WARMUP_TOTAL.labels(result="failed").inc()
    finally:
        with _lock:
            _in_progress = False


def trigger_warm_up(embedder: OllamaEmbedder | None = None) -> bool:
    """Start one warm-up embed call in the background, unless one is already in flight.

    Returns immediately either way: ``True`` if this call actually started a background
    warm-up, ``False`` if one was already running (skipped -- recorded as
    ``secrag_ollama_warmup_total{result="skipped"}`` so the admin panel can tell "we tried
    but another warm-up got there first" apart from silence).
    """
    global _in_progress
    with _lock:
        if _in_progress:
            metrics.OLLAMA_WARMUP_TOTAL.labels(result="skipped").inc()
            return False
        _in_progress = True
    threading.Thread(
        target=_run, args=(embedder or OllamaEmbedder(),), daemon=True, name="ollama-warmup"
    ).start()
    return True
