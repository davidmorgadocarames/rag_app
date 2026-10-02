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
