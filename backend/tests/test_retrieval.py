"""Unit tests for retrieval fusion (no database required)."""

from __future__ import annotations

import pytest

from rag_app.retrieval import RetrievedChunk, reciprocal_rank_fusion


def test_rrf_rewards_agreement_across_lists() -> None:
    vector = ["a", "b", "c"]
    bm25 = ["b", "a", "d"]
    fused = reciprocal_rank_fusion([vector, bm25])
    order = [uid for uid, _ in fused]
    # "a" and "b" appear high in both lists → they rank above single-list "c"/"d"
    assert set(order[:2]) == {"a", "b"}
    assert order[-1] in {"c", "d"}


def test_rrf_top_rank_beats_lower_rank() -> None:
    fused = dict(reciprocal_rank_fusion([["x", "y"]]))
    assert fused["x"] > fused["y"]


def test_rrf_empty_input() -> None:
    assert reciprocal_rank_fusion([]) == []


def test_rrf_k0_damping_is_monotonic() -> None:
    small_k0 = dict(reciprocal_rank_fusion([["x", "y"]], k0=1))
    large_k0 = dict(reciprocal_rank_fusion([["x", "y"]], k0=1000))
    # a larger k0 flattens the gap between rank 1 and rank 2
    assert (small_k0["x"] - small_k0["y"]) > (large_k0["x"] - large_k0["y"])


# --- DA-13-G1-2: direct unit test for hybrid_search's own atomic-step wiring --------------


def _step_counts() -> dict[tuple[str, str], float]:
    """Mirrors ``test_generation.py::_step_counts()`` -- current observation counts per
    (stage, step), read from the REAL process-wide ``metrics.REGISTRY``; tests only ever
    assert a before/after DELTA, so sharing the registry with the rest of the pytest
    session is harmless."""
    from rag_app import metrics as real_metrics

    out: dict[tuple[str, str], float] = {}
    for metric in real_metrics.REGISTRY.collect():
        if metric.name != "secrag_chat_step_seconds":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                out[(sample.labels["stage"], sample.labels["step"])] = sample.value
    return out


def test_hybrid_search_emits_exactly_its_allow_listed_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-13-G1-2: unlike ``test_generation.py``'s atomic-step tests (which monkeypatch
    ``hybrid_search`` away entirely), this calls the REAL ``retrieval.hybrid_search`` --
    only its DB-touching helpers and the embedder are faked -- so a typo in one of its 5
    literal (stage, step) pairs (``metrics.observe_step`` raises on an unlisted pair) would
    fail THIS test directly, instead of only the slow, Ollama+DB-backed e2e chat turn.

    T13.8.9 (DA-13-G1-fix-1): ALSO wraps the real call's own ``timing.stage("embed")``/
    ``timing.stage("hybrid_search")`` (the two internal stages ``hybrid_search`` already
    opens -- UI pipeline stage "retrieving" spans both) via ``on_stage`` and asserts the real
    step-sum is within a tight tolerance of the real stage total -- the synthetic
    ``test_metrics.py`` sum test can't catch a regression of the 13-G1-fix (e.g.
    ``load_chunks`` moved back outside ``hybrid_search``'s own ``with`` block) because it
    never calls this real code path; this test now would."""
    import time

    import rag_app.retrieval as retrieval
    from rag_app import metrics as real_metrics
    from rag_app import timing

    monkeypatch.setattr(retrieval, "_vector_uids", lambda *_a, **_k: ["a", "b"])
    monkeypatch.setattr(retrieval, "_bm25_uids", lambda *_a, **_k: ["b", "c"])

    def _fake_load_chunks(_session: object, uids: list[str]) -> dict[str, RetrievedChunk]:
        # A deliberate, measurable delay: with trivial fake data every step is otherwise
        # sub-millisecond, so a genuine "this step fell outside the stage span" regression
        # (DA-13-G1-1's own load_chunks case) would be invisible against timer noise without
        # it.
        time.sleep(0.05)
        return {
            uid: RetrievedChunk(
                chunk_uid=uid, heading="h", text="t", version="2021", effective_date=None, score=0.0
            )
            for uid in uids
        }

    monkeypatch.setattr(retrieval, "_load_chunks", _fake_load_chunks)

    class _FakeEmbedder:
        def embed_one(self, _query: str) -> list[float]:
            return [0.1, 0.2]

    step_durations: dict[tuple[str, str], float] = {}

    def _capturing_on_step(stage: str, step: str, elapsed_s: float) -> None:
        step_durations[(stage, step)] = step_durations.get((stage, step), 0.0) + elapsed_s
        real_metrics.observe_step(stage, step, elapsed_s)

    stage_totals: dict[str, float] = {}

    def _capturing_on_stage(stage: str, elapsed_s: float) -> None:
        stage_totals[stage] = stage_totals.get(stage, 0.0) + elapsed_s

    before = _step_counts()
    with timing.recorder(streaming=False, on_step=_capturing_on_step, on_stage=_capturing_on_stage):
        chunks = retrieval.hybrid_search(None, "q", embedder=_FakeEmbedder())  # type: ignore[arg-type]
    after = _step_counts()

    assert [c.chunk_uid for c in chunks]  # the fakes above produced at least one chunk
    retrieving_pairs = (
        ("retrieving", "embed_query"),
        ("retrieving", "vector_search"),
        ("retrieving", "bm25_search"),
        ("retrieving", "rrf_fusion"),
        ("retrieving", "load_chunks"),
    )
    for pair in retrieving_pairs:
        assert after.get(pair, 0.0) > before.get(pair, 0.0), pair

    # DA-13-G1-1/13-G1-fix: the "retrieving" UI stage's step-sum must equal the sum of the
    # TWO real internal stages hybrid_search opens (embed + hybrid_search) -- never diverge by
    # more than context-manager overhead (a few ms on a loaded CI box, nowhere near a real
    # untracked-gap regression, which would be the whole load_chunks step's own duration). The
    # injected 0.05s delay on load_chunks makes this assertion actually discriminate.
    real_step_sum = sum(step_durations.get(pair, 0.0) for pair in retrieving_pairs)
    real_stage_total = stage_totals.get("embed", 0.0) + stage_totals.get("hybrid_search", 0.0)
    assert step_durations.get(("retrieving", "load_chunks"), 0.0) >= 0.04  # the delay landed
    assert real_step_sum == pytest.approx(real_stage_total, abs=0.02)
