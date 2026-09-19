"""Unit tests for retrieval fusion (no database required)."""

from __future__ import annotations

from rag_app.retrieval import reciprocal_rank_fusion


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
