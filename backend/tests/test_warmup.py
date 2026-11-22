"""``rag_app.warmup`` (T13.13.1/T13.13.2, DEBT-061; DEC-050/T13.J4-fix narrowed the call
sites): a background warm-up embed call to Ollama that must be non-blocking,
failure-tolerant and single-flight -- fired from the API lifespan (once, at start-up), a
SUCCESSFUL login and an authenticated ``/chat`` page load (``GET /conversations``) --
never any more from the unauthenticated ``GET /wake`` (see test_wake.py's own negative
control). No live Ollama needed: ``OllamaEmbedder.embed_one`` is replaced by a fake that
lets each test control timing precisely (an ``Event`` the fake blocks on, released on
demand).
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from rag_app import metrics, warmup


@pytest.fixture(autouse=True)
def _reset_single_flight_guard() -> None:
    """Every test starts from a clean guard -- a previous test's background thread (if any)
    has already released it by the time its own test function returned (each test below
    joins/waits on its fake calls before finishing)."""
    warmup._in_progress = False


class _BlockingEmbedder:
    """A fake ``OllamaEmbedder`` whose ``embed_one`` blocks on ``release`` and records every
    call it was asked to make (``calls``), so a test can prove "only one call happened while
    blocked" without a real network call or a real multi-second Ollama wake."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self.release = threading.Event()
        self.started = threading.Event()
        self.raises = raises

    def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        if self.raises:
            raise RuntimeError("ollama unreachable")
        return [0.0]


class _ImmediateEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.0]


def _wait_until_idle(timeout: float = 2.0) -> None:
    """Polls ``warmup._in_progress`` back to False -- the background thread's own signal
    that ``_run`` has returned (success or failure)."""
    deadline = time.monotonic() + timeout
    while warmup._in_progress and time.monotonic() < deadline:
        time.sleep(0.01)


def test_trigger_warm_up_calls_embed_one() -> None:
    embedder = _ImmediateEmbedder()
    started = warmup.trigger_warm_up(embedder)  # type: ignore[arg-type]
    assert started is True
    _wait_until_idle()
    assert embedder.calls == ["warm-up"]


def test_trigger_warm_up_is_non_blocking() -> None:
    """Red on a synchronous (non-background) implementation: calling ``trigger_warm_up``
    must return almost immediately even though the embed call itself is still blocked."""
    embedder = _BlockingEmbedder()
    start = time.monotonic()
    started = warmup.trigger_warm_up(embedder)  # type: ignore[arg-type]
    elapsed = time.monotonic() - start
    assert started is True
    assert elapsed < 0.5, "trigger_warm_up blocked on the embed call instead of backgrounding it"
    embedder.release.set()
    _wait_until_idle()


def test_a_second_call_while_one_is_in_flight_is_skipped_not_queued() -> None:
    embedder = _BlockingEmbedder()
    first = warmup.trigger_warm_up(embedder)  # type: ignore[arg-type]
    assert first is True
    embedder.started.wait(timeout=2)  # the first call has actually started

    second = warmup.trigger_warm_up(embedder)  # type: ignore[arg-type]
    assert second is False, "a warm-up already in flight must be skipped, never queued"
    assert embedder.calls == 1, "the single-flight guard must prevent a second concurrent call"

    embedder.release.set()
    _wait_until_idle()


def test_a_failing_embed_is_only_logged_never_raised(caplog: pytest.LogCaptureFixture) -> None:
    embedder = _BlockingEmbedder(raises=True)
    with caplog.at_level(logging.WARNING, logger="rag_app.warmup"):
        started = warmup.trigger_warm_up(embedder)  # type: ignore[arg-type]
        assert started is True  # trigger_warm_up itself never raises
        embedder.release.set()
        _wait_until_idle()
    assert "ollama warm-up embed failed" in caplog.text


def test_the_guard_releases_again_after_a_failure() -> None:
    """A failed warm-up must not leave the single-flight guard stuck "in progress" forever
    -- the NEXT call (e.g. the following page load's wake ping) must still be able to run."""
    failing = _BlockingEmbedder(raises=True)
    warmup.trigger_warm_up(failing)  # type: ignore[arg-type]
    failing.release.set()
    _wait_until_idle()

    embedder = _ImmediateEmbedder()
    started = warmup.trigger_warm_up(embedder)  # type: ignore[arg-type]
    assert started is True
    _wait_until_idle()
    assert embedder.calls == ["warm-up"]


def test_results_are_counted_on_the_dedicated_metric_never_the_chat_histograms() -> None:
    """T13.13.1's own docstring: a warm-up must never be averaged into a real answer's
    ``secrag_chat_stage_seconds``/``secrag_chat_step_seconds`` -- it has its own counter."""
    before_ok = metrics.OLLAMA_WARMUP_TOTAL.labels(result="ok")._value.get()
    before_failed = metrics.OLLAMA_WARMUP_TOTAL.labels(result="failed")._value.get()
    before_skipped = metrics.OLLAMA_WARMUP_TOTAL.labels(result="skipped")._value.get()

    ok_embedder = _ImmediateEmbedder()
    warmup.trigger_warm_up(ok_embedder)  # type: ignore[arg-type]
    _wait_until_idle()
    assert metrics.OLLAMA_WARMUP_TOTAL.labels(result="ok")._value.get() == before_ok + 1

    blocking = _BlockingEmbedder(raises=True)
    warmup.trigger_warm_up(blocking)  # type: ignore[arg-type]
    blocking.started.wait(timeout=2)
    warmup.trigger_warm_up(blocking)  # skipped while the first is still in flight
    blocking.release.set()
    _wait_until_idle()

    assert metrics.OLLAMA_WARMUP_TOTAL.labels(result="failed")._value.get() == before_failed + 1
    assert metrics.OLLAMA_WARMUP_TOTAL.labels(result="skipped")._value.get() == before_skipped + 1


def test_the_metric_has_only_a_result_label() -> None:
    assert metrics.OLLAMA_WARMUP_TOTAL._labelnames == ("result",)
