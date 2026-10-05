"""Unit tests for the latency baseline module (T11.3.5): percentile math, stack-down
behaviour (mirrors ``test_eval.py``'s eval-gate test) and stage aggregation with the real
reranker/model calls monkeypatched out (no DB/Ollama needed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rag_app import generation, reranking, timing
from rag_app.eval import latency
from rag_app.generation import Answer
from rag_app.retrieval import RetrievedChunk


def test_percentile_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert latency._percentile(values, 0) == 10.0
    assert latency._percentile(values, 50) == 30.0
    assert latency._percentile(values, 100) == 50.0


def test_percentile_empty_is_zero() -> None:
    assert latency._percentile([], 95) == 0.0


def test_percentile_does_not_require_sorted_input() -> None:
    assert latency._percentile([30.0, 10.0, 20.0], 50) == 20.0


def test_latency_gate_skips_or_fails_when_the_stack_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(latency, "_stack_available", lambda: False)
    assert latency.main([]) == 0  # by hand: SKIP
    assert latency.main(["--require-stack"]) == 1  # gate --full: FAIL


def test_machine_info_has_the_documented_fields() -> None:
    info = latency.machine_info(reranker_device="cpu")
    assert info["reranker_device"] == "cpu"
    assert info["reranker_mode"] == "shared"  # default
    for key in (
        "cpu",
        "cpu_count",
        "ram_gib",
        "gpu",
        "reranker_mode",
        "llm_model",
        "embed_model",
        "reranker_model",
        "reranker_revision",
        "top_k",
        "rerank_top_n",
        "num_ctx_answer",
        "num_ctx_groundedness",
        "commit",
    ):
        assert key in info


def test_machine_info_records_an_explicit_reranker_mode() -> None:
    info = latency.machine_info(reranker_device="cpu", reranker_mode="fresh")
    assert info["reranker_mode"] == "fresh"


def _fake_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid="c::1", heading="h", text="t", version="v", effective_date=None, score=1.0
    )


def test_run_latency_benchmark_aggregates_every_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real call shape ``generation.answer_question`` uses (a fresh
    ``CrossEncoderReranker()`` per question via ``reranking.retrieve``, then
    ``answer_from_chunks``) with the actual model calls stubbed: stage durations still flow
    through the real ``timing`` contextvar machinery into THIS benchmark's own ``on_stage``
    callback (not nested inside another recorder — see ``run_latency_benchmark``'s
    docstring)."""

    def fake_retrieve(*_args: Any, **_kwargs: Any) -> list[RetrievedChunk]:
        with timing.stage("rerank_load"):
            pass
        with timing.stage("rerank_inference"):
            pass
        return [_fake_chunk()]

    def fake_answer_from_chunks(*_args: Any, **_kwargs: Any) -> Answer:
        with timing.stage("generate"):
            pass
        with timing.stage("groundedness"):
            pass
        return Answer(text="ok [1]", citations=[], abstained=False, grounded=True)

    monkeypatch.setattr(reranking, "retrieve", fake_retrieve)
    monkeypatch.setattr(generation, "answer_from_chunks", fake_answer_from_chunks)
    monkeypatch.setattr(latency, "OllamaChat", lambda: object())

    items = [
        type("Item", (), {"id": f"q{i}", "question": f"question {i}", "version": None})()
        for i in range(3)
    ]
    report = latency.run_latency_benchmark(
        session=None,  # type: ignore[arg-type]
        items=items,  # type: ignore[arg-type]
        n_runs=2,
        reranker_device="cpu",
        reranker_mode="fresh",  # retrieve() is fully stubbed below; "shared" would warm a
        # real model via _reranker_factory before ever reaching the stub
    )

    assert report.n_runs == 2
    assert report.n_questions == 3
    expected_stages = {"rerank_load", "rerank_inference", "generate", "groundedness", "total"}
    assert expected_stages <= set(report.stages_ms)
    for name in expected_stages:
        stats = report.stages_ms[name]
        assert stats["n"] == 6  # 2 runs x 3 questions
        assert stats["p50"] >= 0.0
        assert stats["p95"] >= stats["p50"]
    assert report.machine["reranker_device"] == "cpu"
    json_payload = report.to_json()
    assert json_payload["n_runs"] == 2 and "stages_ms" in json_payload


class _NullSessionCtx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def test_main_writes_the_baseline_json_with_require_stack_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out_path = tmp_path / "latency_baseline.json"

    def fake_run(
        session: object,
        items: object,
        *,
        n_runs: int,
        reranker_device: str,
        reranker_mode: str = "shared",
    ) -> latency.LatencyReport:
        return latency.LatencyReport(
            machine={"reranker_device": reranker_device},
            n_runs=n_runs,
            n_questions=1,
            runtime_s=0.1,
            stages_ms={"total": {"n": 1, "mean": 1.0, "p50": 1.0, "p95": 1.0}},
        )

    monkeypatch.setattr(latency, "_stack_available", lambda: True)
    monkeypatch.setattr(latency, "run_latency_benchmark", fake_run)
    monkeypatch.setattr(latency, "make_session_factory", lambda: (lambda: _NullSessionCtx()))
    # No adopted baseline for this test (T11.6b.1): the routine comparison then FAILS (exit 1)
    # but the JSON-writing plumbing under test still ran first -- see the assertions below.
    monkeypatch.setattr(
        latency, "ADOPTED_BASELINE_PATH", tmp_path / "latency_adopted_baseline.json"
    )

    # --device gpu: avoids this process's CUDA_VISIBLE_DEVICES being mutated as a side
    # effect of the (CPU-labelled) default — irrelevant here, this test only checks the
    # JSON-writing plumbing.
    exit_code = latency.main(
        ["--out", str(out_path), "--require-stack", "--runs", "1", "--device", "gpu"]
    )
    assert exit_code == 1
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["n_runs"] == 1


def test_default_out_path_is_results_not_baseline_unless_update_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A routine run (gate.sh's `step_latency`, no `--update-baseline`) must never touch the
    git-tracked `latency_baseline.json` — otherwise timing jitter dirties the tree on every
    `--full` and the gate-full commit status can never publish (found running this block's
    final gate; same split as eval.gate's results.json/baseline_metrics.json)."""
    fake_baseline = tmp_path / "latency_baseline.json"
    fake_results = tmp_path / "latency_results.json"
    fake_adopted = tmp_path / "latency_adopted_baseline.json"
    monkeypatch.setattr(latency, "BASELINE_PATH", fake_baseline)
    monkeypatch.setattr(latency, "RESULTS_PATH", fake_results)
    monkeypatch.setattr(latency, "ADOPTED_BASELINE_PATH", fake_adopted)
    monkeypatch.setattr(latency, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(latency, "_stack_available", lambda: True)
    monkeypatch.setattr(
        latency,
        "run_latency_benchmark",
        lambda *_a, **_k: latency.LatencyReport(
            machine={}, n_runs=1, n_questions=1, runtime_s=0.1, stages_ms={}
        ),
    )
    monkeypatch.setattr(latency, "make_session_factory", lambda: (lambda: _NullSessionCtx()))

    # No adopted baseline yet -> the routine run FAILS loudly (T11.6b.1: never silently
    # "RECORDED", never silently skipped), but still writes RESULTS_PATH, never BASELINE_PATH.
    assert latency.main(["--require-stack", "--runs", "1", "--device", "gpu"]) == 1
    assert fake_results.exists()
    assert not fake_baseline.exists()

    assert (
        latency.main(["--update-baseline", "--require-stack", "--runs", "1", "--device", "gpu"])
        == 0
    )
    assert fake_baseline.exists()

    # --update-adopted-baseline writes ADOPTED_BASELINE_PATH, not BASELINE_PATH/RESULTS_PATH,
    # and skips the comparison (this run IS the new reference).
    assert (
        latency.main(
            ["--update-adopted-baseline", "--require-stack", "--runs", "1", "--device", "gpu"]
        )
        == 0
    )
    assert fake_adopted.exists()

    # Now that an adopted baseline exists and matches this fake report's (empty) machine/
    # stages, the routine run passes.
    assert latency.main(["--require-stack", "--runs", "1", "--device", "gpu"]) == 0


# --- T11.4.4 (block E): reranker_mode, warm-vs-cold, concurrency -------------------------


class _FakeCrossEncoder:
    """Records every construction; a constant score (ordering is not under test here)."""

    calls: list[int] = []

    def __init__(self, *_a: object, **_k: object) -> None:
        type(self).calls.append(1)

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5 for _ in pairs]


@pytest.fixture()
def fake_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> type[_FakeCrossEncoder]:
    import sentence_transformers

    _FakeCrossEncoder.calls = []
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", _FakeCrossEncoder)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    # These tests prove the load-once/warm-up MECHANISM (T11.4.1), independent of whichever
    # model/precision T11.5.3 currently adopts as the Settings default -- force the
    # sentence_transformers.CrossEncoder path (quantize=True, the adopted default since 11b
    # block F, would bypass this fake entirely).
    monkeypatch.setenv("RERANKER_QUANTIZE", "false")
    return _FakeCrossEncoder


def test_reranker_factory_fresh_constructs_a_new_model_per_question(
    fake_cross_encoder: type[_FakeCrossEncoder],
) -> None:
    factory = latency._reranker_factory("fresh")
    for _ in range(3):
        factory().rerank("q", [_fake_chunk()], top_n=1)  # type: ignore[attr-defined]
    assert len(fake_cross_encoder.calls) == 3  # one brand-new instance every time


def test_reranker_factory_shared_builds_once_and_is_the_process_singleton(
    monkeypatch: pytest.MonkeyPatch, fake_cross_encoder: type[_FakeCrossEncoder]
) -> None:
    import rag_app.reranking as reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)
    factory = latency._reranker_factory("shared")
    for _ in range(3):
        factory().rerank("q", [_fake_chunk()], top_n=1)  # type: ignore[attr-defined]
    assert len(fake_cross_encoder.calls) == 1  # warmed once, reused — matches T11.4.1
    assert factory() is reranking.get_shared_reranker()


def test_reranker_factory_warm_single_builds_once_but_is_not_the_shared_singleton(
    monkeypatch: pytest.MonkeyPatch, fake_cross_encoder: type[_FakeCrossEncoder]
) -> None:
    import rag_app.reranking as reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)
    factory = latency._reranker_factory("warm-single")
    for _ in range(3):
        factory().rerank("q", [_fake_chunk()], top_n=1)  # type: ignore[attr-defined]
    assert len(fake_cross_encoder.calls) == 1  # one throwaway instance, warmed once
    assert reranking._shared_reranker is None  # never touched the production singleton


def test_reranker_factory_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown reranker_mode"):
        latency._reranker_factory("bogus")


def test_measure_warm_vs_cold_first_call_pays_load_later_calls_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA review of block C (checks 2 + 4): the first `.rerank()` on a fresh instance pays
    `rerank_load` (model construction); later calls on the SAME instance never do."""
    import time as real_time

    import sentence_transformers

    from rag_app import retrieval

    class _SlowInitCrossEncoder:
        instances = 0

        def __init__(self, *_a: object, **_k: object) -> None:
            type(self).instances += 1
            real_time.sleep(0.02)  # makes rerank_load clearly > 0 for the first call only

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5 for _ in pairs]

    _SlowInitCrossEncoder.instances = 0
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", _SlowInitCrossEncoder)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    # Force the sentence_transformers.CrossEncoder path -- see fake_cross_encoder's comment.
    monkeypatch.setenv("RERANKER_QUANTIZE", "false")
    monkeypatch.setattr(retrieval, "hybrid_search", lambda *_a, **_k: [_fake_chunk()])

    item = type("Item", (), {"id": "q1", "question": "q?", "version": None})()
    result = latency.measure_warm_vs_cold(session=None, item=item, n_calls=3)  # type: ignore[arg-type]

    assert _SlowInitCrossEncoder.instances == 1  # one instance for the whole check
    assert result["n_candidates"] == 1
    assert result["first_call"]["rerank_load_ms"] > 10.0
    assert len(result["later_calls"]) == 2
    assert all(c["rerank_load_ms"] == 0.0 for c in result["later_calls"])


class _NullCtx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def test_measure_concurrency_pools_rerank_inference_across_threads(
    monkeypatch: pytest.MonkeyPatch, fake_cross_encoder: type[_FakeCrossEncoder]
) -> None:
    """DA-11bC-2: N threads sharing the warmed reranker each contribute their own
    `rerank_inference` samples into one pooled p50/p95 — proves the plumbing (thread-safe
    aggregation, no lost/duplicated samples), not the real model's CPU contention number
    (measured separately, by hand, against the real model for the ADR)."""
    import rag_app.reranking as reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)
    monkeypatch.setattr(reranking, "hybrid_search", lambda *_a, **_k: [_fake_chunk()])

    items = [
        type("Item", (), {"id": f"q{i}", "question": f"question {i}", "version": None})()
        for i in range(2)
    ]

    result = latency.measure_concurrency(lambda: _NullCtx(), items, concurrency=4)  # type: ignore[arg-type]

    assert result["concurrency"] == 4
    assert result["n"] == 8  # 4 threads x 2 items
    assert result["p50"] >= 0.0
    assert result["p95"] >= result["p50"]
    assert result["wall_s"] >= 0.0


def test_measure_concurrency_surfaces_a_worker_exception(
    monkeypatch: pytest.MonkeyPatch, fake_cross_encoder: type[_FakeCrossEncoder]
) -> None:
    import rag_app.reranking as reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)

    def _boom(*_a: object, **_k: object) -> list[RetrievedChunk]:
        raise RuntimeError("retrieval exploded")

    monkeypatch.setattr(reranking, "hybrid_search", _boom)
    items = [type("Item", (), {"id": "q1", "question": "q?", "version": None})()]

    with pytest.raises(RuntimeError, match="retrieval exploded"):
        latency.measure_concurrency(lambda: _NullCtx(), items, concurrency=2)  # type: ignore[arg-type]


def test_main_concurrency_flag_writes_its_own_file_instead_of_the_normal_benchmark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    concurrency_path = tmp_path / "latency_concurrency.json"
    monkeypatch.setattr(latency, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(latency, "CONCURRENCY_PATH", concurrency_path)
    monkeypatch.setattr(latency, "_stack_available", lambda: True)
    monkeypatch.setattr(latency, "make_session_factory", lambda: (lambda: _NullSessionCtx()))
    recorded: dict[str, object] = {"concurrency": 3, "n": 6, "p50": 1.0, "p95": 2.0, "wall_s": 0.1}
    monkeypatch.setattr(latency, "measure_concurrency", lambda *_a, **_k: recorded)

    assert latency.main(["--require-stack", "--concurrency", "3"]) == 0
    assert json.loads(concurrency_path.read_text(encoding="utf-8")) == recorded


def test_main_warm_vs_cold_flag_writes_its_own_file_instead_of_the_normal_benchmark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out_path = tmp_path / "latency_warm_vs_cold.json"
    monkeypatch.setattr(latency, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(latency, "WARM_VS_COLD_PATH", out_path)
    monkeypatch.setattr(latency, "_stack_available", lambda: True)
    monkeypatch.setattr(latency, "make_session_factory", lambda: (lambda: _NullSessionCtx()))
    recorded = {
        "n_candidates": 5,
        "first_call": {"call": 1, "rerank_load_ms": 12.0, "rerank_inference_ms": 3.0},
        "later_calls": [],
        "later_mean_inference_ms": 0.0,
    }
    monkeypatch.setattr(latency, "measure_warm_vs_cold", lambda *_a, **_k: recorded)

    assert latency.main(["--require-stack", "--warm-vs-cold"]) == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) == recorded


# --- T11.6b.1 (block G): the real pass/fail check against the adopted baseline -----------


def _report(
    stages_ms: dict[str, dict[str, float]], machine: dict[str, object] | None = None
) -> latency.LatencyReport:
    return latency.LatencyReport(
        machine=machine or {}, n_runs=1, n_questions=1, runtime_s=0.1, stages_ms=stages_ms
    )


def _adopted_baseline(
    stages_ms: dict[str, dict[str, float]], machine: dict[str, object] | None = None
) -> dict[str, object]:
    return {"machine": machine or {}, "stages_ms": stages_ms}


def test_check_against_adopted_baseline_passes_within_tolerance() -> None:
    baseline = _adopted_baseline(
        {"rerank_inference": {"n": 1, "mean": 1000.0, "p50": 1000.0, "p95": 1024.5}}
    )
    report = _report({"rerank_inference": {"n": 1, "mean": 1100.0, "p50": 1100.0, "p95": 1100.0}})

    result = latency.check_against_adopted_baseline(report, baseline)

    assert result.ok
    assert result.failures == []


def test_check_against_adopted_baseline_fails_on_an_artificial_slowdown() -> None:
    """Proves the step actually catches a regression: a stage whose p95 blows well past the
    adopted baseline's own tolerance fails, with a human-readable reason naming the stage."""
    baseline = _adopted_baseline(
        {"rerank_inference": {"n": 1, "mean": 1000.0, "p50": 1000.0, "p95": 1024.5}}
    )
    # An artificial slowdown: ~10x the adopted baseline's p95, far outside the +50%/+25ms
    # tolerance.
    report = _report(
        {"rerank_inference": {"n": 1, "mean": 10000.0, "p50": 10000.0, "p95": 10245.0}}
    )

    result = latency.check_against_adopted_baseline(report, baseline)

    assert not result.ok
    assert any("rerank_inference" in f for f in result.failures)


def test_check_against_adopted_baseline_enforces_the_absolute_rerank_floor_regardless_of_tolerance() -> (  # noqa: E501
    None
):
    """Even a stage that stays WITHIN the relative tolerance of a (hypothetically loose)
    adopted baseline must still fail if rerank_inference's own p95 crosses the absolute 1.5s
    floor (DA-11bE-2) -- the floor is a hard ceiling, never loosened by the baseline."""
    from rag_app.eval.rerank_matrix import FLOOR_ABSOLUTE_MS

    loose_p95 = FLOOR_ABSOLUTE_MS * 1.2  # the baseline itself is already above the floor
    baseline = _adopted_baseline(
        {"rerank_inference": {"n": 1, "mean": loose_p95, "p50": loose_p95, "p95": loose_p95}}
    )
    # Within +50%/+25ms of the (loose) baseline, but still over the absolute floor.
    report = _report(
        {"rerank_inference": {"n": 1, "mean": loose_p95, "p50": loose_p95, "p95": loose_p95}}
    )

    result = latency.check_against_adopted_baseline(report, baseline)

    assert not result.ok
    assert any("absolute" in f for f in result.failures)


def test_check_against_adopted_baseline_fails_on_a_config_mismatch() -> None:
    """A live config that no longer matches what the adopted baseline was measured with
    (e.g. top_k changed without re-adopting a baseline) must fail loudly, not silently compare
    incompatible numbers."""
    baseline = _adopted_baseline(
        {},
        machine={
            "reranker_model": "BAAI/bge-reranker-base",
            "reranker_revision": "2cfc18c9415c912f9d8155881c133215df768a70",
            "top_k": 10,
            "rerank_top_n": 4,
        },
    )
    report = _report(
        {},
        machine={
            "reranker_model": "BAAI/bge-reranker-base",
            "reranker_revision": "2cfc18c9415c912f9d8155881c133215df768a70",
            "top_k": 20,
            "rerank_top_n": 4,
        },
    )

    result = latency.check_against_adopted_baseline(report, baseline)

    assert not result.ok
    assert any("config mismatch" in f and "top_k" in f for f in result.failures)


def test_main_fails_without_an_adopted_baseline_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The step must FAIL (not silently pass/skip) when it has nothing to compare against."""
    monkeypatch.setattr(latency, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(latency, "RESULTS_PATH", tmp_path / "latency_results.json")
    monkeypatch.setattr(
        latency, "ADOPTED_BASELINE_PATH", tmp_path / "latency_adopted_baseline.json"
    )
    monkeypatch.setattr(latency, "_stack_available", lambda: True)
    monkeypatch.setattr(
        latency,
        "run_latency_benchmark",
        lambda *_a, **_k: _report(
            {"rerank_inference": {"n": 1, "mean": 1.0, "p50": 1.0, "p95": 1.0}}
        ),
    )
    monkeypatch.setattr(latency, "make_session_factory", lambda: (lambda: _NullSessionCtx()))

    assert latency.main(["--require-stack", "--runs", "1", "--device", "gpu"]) == 1


def test_main_fails_when_the_run_itself_is_an_artificial_slowdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end (through main(), as gate.sh calls it): a fast, committed adopted baseline
    plus a run that comes back 10x slower on rerank_inference fails the whole step."""
    adopted_path = tmp_path / "latency_adopted_baseline.json"
    adopted_path.write_text(
        json.dumps(
            _adopted_baseline(
                {"rerank_inference": {"n": 1, "mean": 1000.0, "p50": 1000.0, "p95": 1024.5}}
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(latency, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(latency, "RESULTS_PATH", tmp_path / "latency_results.json")
    monkeypatch.setattr(latency, "ADOPTED_BASELINE_PATH", adopted_path)
    monkeypatch.setattr(latency, "_stack_available", lambda: True)
    monkeypatch.setattr(
        latency,
        "run_latency_benchmark",
        lambda *_a, **_k: _report(
            {"rerank_inference": {"n": 1, "mean": 10000.0, "p50": 10000.0, "p95": 10245.0}}
        ),
    )
    monkeypatch.setattr(latency, "make_session_factory", lambda: (lambda: _NullSessionCtx()))

    assert latency.main(["--require-stack", "--runs", "1", "--device", "gpu"]) == 1
