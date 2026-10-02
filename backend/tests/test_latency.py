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
    for key in (
        "cpu",
        "cpu_count",
        "ram_gib",
        "gpu",
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
        session: object, items: object, *, n_runs: int, reranker_device: str
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

    # --device gpu: avoids this process's CUDA_VISIBLE_DEVICES being mutated as a side
    # effect of the (CPU-labelled) default — irrelevant here, this test only checks the
    # JSON-writing plumbing.
    exit_code = latency.main(
        ["--out", str(out_path), "--require-stack", "--runs", "1", "--device", "gpu"]
    )
    assert exit_code == 0
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
    monkeypatch.setattr(latency, "BASELINE_PATH", fake_baseline)
    monkeypatch.setattr(latency, "RESULTS_PATH", fake_results)
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

    assert latency.main(["--require-stack", "--runs", "1", "--device", "gpu"]) == 0
    assert fake_results.exists()
    assert not fake_baseline.exists()

    assert (
        latency.main(["--update-baseline", "--require-stack", "--runs", "1", "--device", "gpu"])
        == 0
    )
    assert fake_baseline.exists()
