"""Unit tests for the per-stage answer timing recorder (T11.3.1).

No live LLM/DB needed: these exercise the recorder/stage/mark_first_token machinery and
the JSON record it emits directly.
"""

from __future__ import annotations

import json
import logging

import pytest

from rag_app import timing


def test_stage_is_a_no_op_without_an_active_recorder() -> None:
    """Called outside any recorder (eval/agentic callers, T11.3.1): just runs the block."""
    ran = False
    with timing.stage("embed"):
        ran = True
    assert ran
    timing.mark_first_token()  # must not raise
    timing.record("generate", 0.1)  # must not raise


def test_recorder_emits_one_json_record_with_every_stage(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    with timing.recorder(streaming=False):
        with timing.stage("embed"):
            pass
        with timing.stage("hybrid_search"):
            pass
        with timing.stage("generate"):
            pass
        timing.mark_first_token()

    records = [r for r in caplog.records if r.name == "rag_app.timing"]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["event"] == "answer_timing"
    assert payload["streaming"] is False
    assert set(payload) >= {"embed_ms", "hybrid_search_ms", "generate_ms", "ttft_ms", "total_ms"}
    assert payload["ttft_ms"] is not None
    numeric = {k: v for k, v in payload.items() if k not in ("event", "streaming")}
    assert all(isinstance(v, int | float) or v is None for v in numeric.values())


def test_ttft_is_none_when_nothing_streamed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    with timing.recorder(streaming=False):
        pass
    payload = json.loads(next(r for r in caplog.records if r.name == "rag_app.timing").getMessage())
    assert payload["ttft_ms"] is None


def test_mark_first_token_only_records_the_first_call(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    with timing.recorder(streaming=True):
        timing.mark_first_token()
        first = timing._current.get().ttft_s  # type: ignore[union-attr]
        timing.mark_first_token()
        second = timing._current.get().ttft_s  # type: ignore[union-attr]
    assert first == second


def test_repeated_stage_calls_accumulate() -> None:
    with timing.recorder(streaming=False) as rec:
        with timing.stage("rerank_inference"):
            pass
        with timing.stage("rerank_inference"):
            pass
    assert "rerank_inference" in rec.durations_s


def test_on_stage_callback_sees_every_stage_plus_total_and_ttft() -> None:
    seen: list[tuple[str, float]] = []
    with timing.recorder(streaming=True, on_stage=lambda name, secs: seen.append((name, secs))):
        with timing.stage("classify"):
            pass
        timing.mark_first_token()
    names = [n for n, _ in seen]
    assert "classify" in names
    assert "ttft" in names
    assert "total" in names


def test_no_pii_in_the_timing_record(caplog: pytest.LogCaptureFixture) -> None:
    """The JSON record never carries the question text, a user id or an answer (T11.3.1)."""
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    secret_question = "my super secret question about my-email@example.com"
    with timing.recorder(streaming=False):
        with timing.stage("generate"):
            pass
    line = next(r for r in caplog.records if r.name == "rag_app.timing").getMessage()
    assert secret_question not in line
    assert "@" not in line
    payload = json.loads(line)
    allowed_keys = {"event", "streaming", "total_ms", "ttft_ms"} | {
        f"{s}_ms"
        for s in (
            "classify",
            "embed",
            "hybrid_search",
            "rerank_load",
            "rerank_inference",
            "generate",
            "groundedness",
        )
    }
    assert set(payload) <= allowed_keys


def test_step_is_a_no_op_without_an_active_recorder() -> None:
    """T13.8.1: mirrors ``test_stage_is_a_no_op_without_an_active_recorder`` for steps."""
    ran = False
    with timing.step("retrieving", "embed_query"):
        ran = True
    assert ran
    timing.record_step("retrieving", "embed_query", 0.1)  # must not raise


def test_step_is_a_no_op_with_a_recorder_but_no_on_step_callback() -> None:
    """A recorder opened with only ``on_stage`` (every pre-13-G1 caller) never crashes on a
    ``step()``/``record_step()`` call -- it is simply never forwarded anywhere."""
    with timing.recorder(streaming=False, on_stage=lambda *_a: None):
        with timing.step("retrieving", "embed_query"):
            pass
        timing.record_step("retrieving", "embed_query", 0.1)


def test_on_step_callback_sees_stage_step_and_elapsed_seconds() -> None:
    seen: list[tuple[str, str, float]] = []

    def on_step(stage_name: str, step_name: str, secs: float) -> None:
        seen.append((stage_name, step_name, secs))

    with timing.recorder(streaming=False, on_step=on_step):
        with timing.step("retrieving", "embed_query"):
            pass
        timing.record_step("retrieving", "vector_search", 0.05)
    assert seen[0][:2] == ("retrieving", "embed_query")
    assert seen[1] == ("retrieving", "vector_search", 0.05)


def test_step_durations_never_reach_durations_s_or_the_json_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T13.8.1: steps are a SEPARATE channel from stages -- they must never pollute
    ``durations_s`` (and therefore never appear in the per-answer JSON log), even with an
    ``on_step`` callback wired up."""
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    with timing.recorder(streaming=False, on_step=lambda *_a: None) as rec:
        with timing.step("retrieving", "embed_query"):
            pass
        timing.record_step("retrieving", "vector_search", 0.05)
    assert rec.durations_s == {}
    payload = json.loads(next(r for r in caplog.records if r.name == "rag_app.timing").getMessage())
    assert "embed_query" not in json.dumps(payload)
    assert "vector_search" not in json.dumps(payload)


def test_install_timing_log_is_idempotent_and_sets_info_level() -> None:
    timing.install_timing_log()
    handlers_after_first = list(timing.logger.handlers)
    timing.install_timing_log()
    assert timing.logger.handlers == handlers_after_first  # no duplicate handler
    assert timing.logger.level == logging.INFO
    assert timing.logger.propagate is True  # caplog (root-attached) must still see records


def test_emit_runs_even_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rag_app.timing")
    with pytest.raises(RuntimeError):
        with timing.recorder(streaming=False):
            with timing.stage("generate"):
                raise RuntimeError("boom")
    records = [r for r in caplog.records if r.name == "rag_app.timing"]
    assert len(records) == 1


# --- DA-13-G1-4: an ``on_step`` error must never mask the wrapped block's own exception ----


def test_step_does_not_mask_the_original_exception_when_on_step_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the wrapped block raises AND ``on_step`` (``metrics.observe_step`` in production,
    which raises on an unlisted (stage, step) pair) ALSO raises while recording the step in
    ``step()``'s ``finally``, the ORIGINAL exception must still be what propagates -- a
    secondary metrics bug must never replace/hide a real pipeline error."""
    caplog.set_level(logging.ERROR, logger="rag_app.timing")

    def exploding_on_step(*_a: object) -> None:
        raise ValueError("observe_step exploded")

    with pytest.raises(RuntimeError, match="boom"):
        with timing.recorder(streaming=False, on_step=exploding_on_step):
            with timing.step("retrieving", "embed_query"):
                raise RuntimeError("boom")
    # the secondary failure is logged, not silently swallowed either.
    errors = [
        r for r in caplog.records if r.name == "rag_app.timing" and r.levelno >= logging.ERROR
    ]
    assert any("failed to record step" in r.getMessage() for r in errors)


def test_step_still_records_normally_when_on_step_does_not_raise() -> None:
    """Sanity check alongside the test above: the try/except added for DA-13-G1-4 must not
    change the normal (no secondary exception) recording behaviour."""
    seen: list[tuple[str, str, float]] = []
    with timing.recorder(streaming=False, on_step=lambda *a: seen.append(a)):
        with timing.step("retrieving", "embed_query"):
            pass
    assert seen and seen[0][:2] == ("retrieving", "embed_query")
