"""Unit tests for rerank ordering (no torch/model required)."""

from __future__ import annotations

import pytest

from rag_app.reranking import order_by_scores
from rag_app.retrieval import RetrievedChunk


def _chunk(uid: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_uid=uid,
        heading="",
        text=f"text {uid}",
        version="2021",
        effective_date=None,
        score=0.0,
    )


def test_order_by_scores_sorts_desc_and_truncates() -> None:
    candidates = [_chunk("a"), _chunk("b"), _chunk("c")]
    ranked = order_by_scores(candidates, [0.1, 0.9, 0.5], top_n=2)
    assert [c.chunk_uid for c in ranked] == ["b", "c"]
    assert ranked[0].score == 0.9


def test_order_by_scores_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="same length"):
        order_by_scores([_chunk("a")], [0.1, 0.2], top_n=1)


def test_order_by_scores_top_n_larger_than_input() -> None:
    ranked = order_by_scores([_chunk("a")], [0.3], top_n=10)
    assert len(ranked) == 1


# --- pinned revision + offline loading (DA-B-7, PHASE_TASKS row 37d) ---------------------

PINNED = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def test_the_configured_reranker_is_pinned_to_a_commit() -> None:
    from rag_app.config import get_settings
    from rag_app.reranking import CrossEncoderReranker

    reranker = CrossEncoderReranker()
    assert reranker.revision == get_settings().reranker_revision == PINNED


@pytest.mark.parametrize("revision", ["main", "v1.0", PINNED[:12], PINNED.upper()])
def test_a_branch_tag_or_short_hash_is_refused(revision: str) -> None:
    from rag_app.reranking import CrossEncoderReranker, RerankerModelError

    with pytest.raises(RerankerModelError, match="40-hex commit"):
        CrossEncoderReranker(revision=revision)


def test_another_model_needs_its_own_revision() -> None:
    from rag_app.reranking import CrossEncoderReranker, RerankerModelError

    with pytest.raises(RerankerModelError):
        CrossEncoderReranker(model_name="someone/other-reranker")
    assert CrossEncoderReranker("someone/other-reranker", "a" * 40).revision == "a" * 40


class _FakeCrossEncoder:
    """Records every load; ``cached`` decides whether a local-only load succeeds."""

    calls: list[dict[str, object]] = []
    cached = True

    def __init__(self, name: str, **kwargs: object) -> None:
        type(self).calls.append({"name": name, **kwargs})
        if kwargs.get("local_files_only") and not type(self).cached:
            raise OSError("not in the local cache")


@pytest.fixture()
def fake_st(monkeypatch: pytest.MonkeyPatch) -> type[_FakeCrossEncoder]:
    import sentence_transformers

    _FakeCrossEncoder.calls = []
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", _FakeCrossEncoder)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return _FakeCrossEncoder


def test_a_cached_model_loads_offline_at_the_pinned_revision(fake_st: type) -> None:
    from rag_app.reranking import load_cross_encoder

    fake_st.cached = True
    load_cross_encoder("BAAI/bge-reranker-v2-m3", PINNED)
    assert fake_st.calls == [
        {
            "name": "BAAI/bge-reranker-v2-m3",
            "local_files_only": True,
            "revision": PINNED,
            "trust_remote_code": False,
        }
    ]


def test_a_missing_model_downloads_the_same_commit_only(fake_st: type) -> None:
    from rag_app.reranking import load_cross_encoder

    fake_st.cached = False
    load_cross_encoder("BAAI/bge-reranker-v2-m3", PINNED)
    assert len(fake_st.calls) == 2
    download = fake_st.calls[1]
    assert download["revision"] == PINNED and download["trust_remote_code"] is False
    assert "local_files_only" not in download


def test_offline_mode_never_downloads(fake_st: type, monkeypatch: pytest.MonkeyPatch) -> None:
    from rag_app.reranking import RerankerModelError, load_cross_encoder

    fake_st.cached = False
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(RerankerModelError, match="HF_HUB_OFFLINE"):
        load_cross_encoder("BAAI/bge-reranker-v2-m3", PINNED)
    assert len(fake_st.calls) == 1 and fake_st.calls[0]["local_files_only"] is True
