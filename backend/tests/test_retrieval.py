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
    fail THIS test directly, instead of only the slow, Ollama+DB-backed e2e chat turn."""
    import rag_app.retrieval as retrieval
    from rag_app import metrics as real_metrics
    from rag_app import timing

    monkeypatch.setattr(retrieval, "_vector_uids", lambda *_a, **_k: ["a", "b"])
    monkeypatch.setattr(retrieval, "_bm25_uids", lambda *_a, **_k: ["b", "c"])

    def _fake_load_chunks(_session: object, uids: list[str]) -> dict[str, RetrievedChunk]:
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

    before = _step_counts()
    with timing.recorder(streaming=False, on_step=real_metrics.observe_step):
        chunks = retrieval.hybrid_search(None, "q", embedder=_FakeEmbedder())  # type: ignore[arg-type]
    after = _step_counts()

    assert [c.chunk_uid for c in chunks]  # the fakes above produced at least one chunk
    for pair in (
        ("retrieving", "embed_query"),
        ("retrieving", "vector_search"),
        ("retrieving", "bm25_search"),
        ("retrieving", "rrf_fusion"),
        ("retrieving", "load_chunks"),
    ):
        assert after.get(pair, 0.0) > before.get(pair, 0.0), pair
