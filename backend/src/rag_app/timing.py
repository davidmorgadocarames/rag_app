"""Per-stage latency timing for one answer (T11.3.1).

``generation.py`` opens a :func:`recorder` around one answer (streaming or not);
``retrieval.py`` and ``reranking.py`` wrap their own sub-stages with :func:`stage` — a
no-op when no recorder is active (eval/agentic callers, which measure their own runs
separately), so this module can be imported everywhere without threading a parameter
through every call. When a recorder IS active, ``emit()`` writes exactly one structured
JSON line to the ``rag_app.timing`` logger per answer: stage name -> duration in
milliseconds, plus ``ttft_ms`` (time to first token; ``None`` when nothing streamed) and
``total_ms``. **No PII**: never the question text, an answer, a user id or an IP — only
stage names (a small, fixed vocabulary) and durations.

An optional ``on_stage`` callback (wired to ``rag_app.metrics.observe_stage`` by the
caller, T11.3.2) is invoked for every stage duration AND for ``"total"``/``"ttft"`` at
``emit()`` time, so the Prometheus histograms get exactly the same numbers as the log.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger("rag_app.timing")

OnStage = Callable[[str, float], None]

_log_installed = False


def install_timing_log() -> None:
    """Make the ``rag_app.timing`` JSON lines actually reach the process's logs.

    This app never calls ``logging.basicConfig``; uvicorn's own logging config
    (``uvicorn.config.LOGGING_CONFIG``) only sets up its OWN loggers and leaves the root
    logger unconfigured at its default level (``WARNING``) — so a plain
    ``logger.info(...)`` here would otherwise be silently dropped before any handler is
    even consulted. Setting this logger's own level to INFO and giving it a handler fixes
    that without touching the root logger (``propagate`` stays True, so pytest's
    ``caplog`` — which hooks the root logger — still sees every record in tests).
    Idempotent, like ``rag_app.logsafe.install_log_redaction``; called from
    ``create_app`` and from the Job entry points that answer questions (eval/CLI mains
    do not need it — they print to stdout directly).
    """
    global _log_installed
    if _log_installed:
        return
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _log_installed = True


@dataclass
class TimingRecorder:
    """Accumulates stage durations (seconds) for one answer."""

    streaming: bool
    on_stage: OnStage | None = None
    durations_s: dict[str, float] = field(default_factory=dict)
    ttft_s: float | None = None
    _start: float = field(default_factory=time.perf_counter)

    def add(self, stage: str, elapsed_s: float) -> None:
        self.durations_s[stage] = self.durations_s.get(stage, 0.0) + elapsed_s
        if self.on_stage is not None:
            self.on_stage(stage, elapsed_s)

    def mark_first_token(self) -> None:
        """Record time-to-first-token once (a later call is a no-op)."""
        if self.ttft_s is None:
            self.ttft_s = time.perf_counter() - self._start

    def emit(self) -> None:
        """Log one JSON record and report ``total``/``ttft`` to ``on_stage``."""
        total_s = time.perf_counter() - self._start
        if self.on_stage is not None:
            self.on_stage("total", total_s)
            if self.ttft_s is not None:
                self.on_stage("ttft", self.ttft_s)
        record: dict[str, object] = {
            "event": "answer_timing",
            "streaming": self.streaming,
            "total_ms": round(total_s * 1000, 1),
            "ttft_ms": round(self.ttft_s * 1000, 1) if self.ttft_s is not None else None,
            **{f"{name}_ms": round(secs * 1000, 1) for name, secs in self.durations_s.items()},
        }
        logger.info(json.dumps(record, sort_keys=True))


_current: contextvars.ContextVar[TimingRecorder | None] = contextvars.ContextVar(
    "rag_app_timing_current", default=None
)


@contextmanager
def recorder(*, streaming: bool, on_stage: OnStage | None = None) -> Iterator[TimingRecorder]:
    """Open one timing recorder for the current answer and emit it on exit (always —
    partial data from a failed/interrupted answer is still useful and still carries no PII)."""
    rec = TimingRecorder(streaming=streaming, on_stage=on_stage)
    token = _current.set(rec)
    try:
        yield rec
    finally:
        _current.reset(token)
        rec.emit()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time one stage; a no-op (just runs the block) when no recorder is active."""
    rec = _current.get()
    if rec is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        rec.add(name, time.perf_counter() - start)


def record(name: str, elapsed_s: float) -> None:
    """Record a stage duration measured by the caller (e.g. an incremental stream)."""
    rec = _current.get()
    if rec is not None:
        rec.add(name, elapsed_s)


def mark_first_token() -> None:
    """Mark time-to-first-token on the active recorder (no-op without one)."""
    rec = _current.get()
    if rec is not None:
        rec.mark_first_token()
