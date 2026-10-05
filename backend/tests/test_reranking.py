"""Unit tests for rerank ordering (no torch/model required)."""

from __future__ import annotations

import threading

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
    """The DEFAULT (Settings-driven) reranker is pinned to a real 40-hex commit -- whatever
    model T11.5.3 currently adopts (not hardcoded here: that would break every time the
    adopted model/revision changes, which is exactly what 11b block F just did)."""
    from rag_app.config import get_settings
    from rag_app.reranking import _COMMIT, CrossEncoderReranker

    settings = get_settings()
    reranker = CrossEncoderReranker()
    assert reranker.model_name == settings.reranker_model
    assert reranker.revision == settings.reranker_revision
    assert _COMMIT.match(reranker.revision)  # a real commit hash, never a branch/tag


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

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5 for _ in pairs]


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
            "max_length": 512,
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


# --- T11.4.1: single shared reranker (loaded once, injected everywhere) --------------------


def test_the_cross_encoder_max_length_is_512(fake_st: type[_FakeCrossEncoder]) -> None:
    from rag_app.reranking import RERANKER_MAX_LENGTH, load_cross_encoder

    fake_st.cached = True
    assert RERANKER_MAX_LENGTH == 512
    load_cross_encoder("BAAI/bge-reranker-v2-m3", PINNED)
    assert fake_st.calls[0]["max_length"] == 512


# --- T11.5.1: max_length override + int8-quantized path (11b block F) ----------------------


def test_load_cross_encoder_max_length_is_overridable(fake_st: type[_FakeCrossEncoder]) -> None:
    from rag_app.reranking import load_cross_encoder

    fake_st.cached = True
    load_cross_encoder("BAAI/bge-reranker-v2-m3", PINNED, max_length=256)
    assert fake_st.calls[0]["max_length"] == 256


def test_cross_encoder_reranker_passes_its_max_length_and_quantize_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CrossEncoderReranker(max_length=..., quantize=...)`` overrides the Settings
    defaults for ONE instance -- the experiment matrix builds several in the same process."""
    import rag_app.reranking as reranking

    seen: dict[str, object] = {}

    def fake_load(model_name: str, revision: str, *, max_length: int, quantize: bool) -> object:
        seen.update(
            model_name=model_name, revision=revision, max_length=max_length, quantize=quantize
        )

        class _Fake:
            def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
                return [0.5 for _ in pairs]

        return _Fake()

    monkeypatch.setattr(reranking, "load_cross_encoder", fake_load)
    reranker = reranking.CrossEncoderReranker(
        model_name="BAAI/bge-reranker-v2-m3", revision=PINNED, max_length=256, quantize=True
    )
    reranker.rerank("q", [_chunk("a")], top_n=1)
    assert seen == {
        "model_name": "BAAI/bge-reranker-v2-m3",
        "revision": PINNED,
        "max_length": 256,
        "quantize": True,
    }


def test_quantized_cross_encoder_adapter_applies_sigmoid_and_respects_max_length() -> None:
    """``_QuantizedCrossEncoder`` must report scores on the SAME 0..1 scale as
    ``sentence_transformers.CrossEncoder.predict()`` (``Settings.thin_threshold`` compares
    against it regardless of which path produced the score), and must truncate at its own
    configured ``max_length`` rather than the model's default."""
    from rag_app.reranking import _QuantizedCrossEncoder

    class _FakeTokenizer:
        def __call__(
            self,
            queries: list[str],
            texts: list[str],
            *,
            padding: bool,
            truncation: bool,
            max_length: int,
            return_tensors: str,
        ) -> dict[str, object]:
            assert return_tensors == "pt"
            _FakeTokenizer.last_max_length = max_length
            return {"queries": queries, "texts": texts}

    class _FakeOutput:
        def __init__(self, logits: object) -> None:
            self.logits = logits

    class _FakeModel:
        def __call__(self, **_kwargs: object) -> _FakeOutput:
            import torch

            # Two pairs: a strongly-relevant logit and a strongly-irrelevant one.
            return _FakeOutput(torch.tensor([[8.0], [-8.0]]))

    adapter = _QuantizedCrossEncoder(_FakeTokenizer(), _FakeModel(), max_length=256)
    scores = adapter.predict([("q", "relevant"), ("q", "irrelevant")])

    assert _FakeTokenizer.last_max_length == 256
    assert len(scores) == 2
    assert 0.0 <= scores[1] < 0.5 < scores[0] <= 1.0  # sigmoid-bounded, correctly ordered


def test_warm_up_loads_the_model_and_runs_one_dummy_predict(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    from rag_app.reranking import CrossEncoderReranker

    fake_st.cached = True
    # quantize=False: this test proves the LOAD-ONCE mechanism (T11.4.1), independent of
    # whichever model/precision T11.5.3 currently adopts as the Settings default.
    reranker = CrossEncoderReranker(
        model_name="BAAI/bge-reranker-v2-m3", revision=PINNED, quantize=False
    )
    assert reranker._model is None
    reranker.warm_up()
    assert reranker._model is not None
    assert len(fake_st.calls) == 1  # the model was constructed exactly once

    # A real request afterwards finds the model already loaded (no second construction).
    reranker.rerank("q", [_chunk("a")], top_n=1)
    assert len(fake_st.calls) == 1


def test_get_shared_reranker_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_app.reranking as reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)
    first = reranking.get_shared_reranker()
    second = reranking.get_shared_reranker()
    assert first is second


def test_get_shared_reranker_constructs_the_model_only_once_across_many_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done-when (T11.4.1): one load across N requests, proven by the ``load_cross_encoder``
    call count — not just object identity. Patches ``load_cross_encoder`` directly (not
    ``sentence_transformers.CrossEncoder``): ``get_shared_reranker()`` always uses the
    Settings-driven defaults, whatever model/precision T11.5.3 currently adopts."""
    import rag_app.reranking as reranking

    calls: list[tuple[str, str]] = []

    class _Fake:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5 for _ in pairs]

    def fake_load(model_name: str, revision: str, *, max_length: int, quantize: bool) -> object:
        calls.append((model_name, revision))
        return _Fake()

    monkeypatch.setattr(reranking, "load_cross_encoder", fake_load)
    monkeypatch.setattr(reranking, "_shared_reranker", None)
    for _ in range(5):
        shared = reranking.get_shared_reranker()
        shared.rerank("q", [_chunk("a"), _chunk("b")], top_n=1)
    assert len(calls) == 1


# --- DA-11bC-2: concurrent predict() on the shared reranker -----------------------------


def test_two_concurrent_rerank_calls_on_the_shared_instance_give_correct_independent_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_shared_reranker()``'s double-checked locking only guarantees the model is
    CONSTRUCTED once (T11.4.1) — ``CrossEncoder.predict()`` itself has no lock, and both
    ``/chat`` (FastAPI's threadpool) and ``/chat/stream`` (its own worker thread,
    T11.2.16/DA-G2-2) call it on the exact same instance, so two real requests can enter
    ``predict()`` at once. Two threads call ``.rerank()`` on the SAME shared instance with
    two DIFFERENT queries/candidate pools at the same time: each must get back its own
    correct ranking (no cross-talk), and the fake model's own in-flight counter proves the
    two calls genuinely overlapped — not serialized one after another by some hidden lock
    this test would otherwise not catch."""
    import threading
    import time

    import rag_app.reranking as reranking

    monkeypatch.setattr(reranking, "_shared_reranker", None)

    active = 0
    max_active = 0
    lock = threading.Lock()

    class _SlowFakeCrossEncoder:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.1)  # releases the GIL — long enough for the other thread to join
                # Score derived from BOTH the query and the candidate text, so a thread
                # that received the OTHER thread's pairs (cross-talk) would score wrong.
                return [1.0 if query[-1] == candidate[-1] else 0.0 for query, candidate in pairs]
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _SlowFakeCrossEncoder())

    results: dict[str, list[str]] = {}
    errors: list[BaseException] = []

    def ask(label: str) -> None:
        try:
            shared = reranking.get_shared_reranker()
            candidates = [_chunk(f"{label}-match"), _chunk(f"{label}-other")]
            # tag each candidate's text so predict() can tell a correct pair from a
            # cross-contaminated one (candidate text ends in the SAME label as the query)
            candidates[0].text = f"text ends in {label}"
            candidates[1].text = "text ends in z"
            query = f"question ends in {label}"
            ranked = shared.rerank(query, candidates, top_n=2)
            results[label] = [c.chunk_uid for c in ranked]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ask, args=(label,)) for label in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert max_active >= 2, "the two predict() calls never actually overlapped"
    # each thread's OWN matching candidate must rank first — no cross-talk between threads
    assert results["a"][0] == "a-match"
    assert results["b"][0] == "b-match"


# --- T11.5.1b: rerank concurrency cap (DA-11bE-3) -------------------------------------------


class _SlowCountingFake:
    """A fake cross-encoder whose ``predict()`` sleeps (releases the GIL) and tracks the
    high-water mark of simultaneously-active calls across ALL instances of one test's run,
    for proving a concurrency bound. Reset ``max_active``/``active`` per test."""

    sleep_s: float = 0.08
    active = 0
    max_active = 0
    lock = threading.Lock()

    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        import time

        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
        try:
            time.sleep(cls.sleep_s)
            return [0.5 for _ in pairs]
        finally:
            with cls.lock:
                cls.active -= 1


def test_concurrency_cap_bounds_simultaneous_predict_calls_but_still_allows_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 callers, default cap (2, Settings.rerank_concurrency): at most 2 ``predict()``
    calls run at once, but more than 1 — proven bounded, not accidentally serialized to 1."""
    import threading

    import rag_app.reranking as reranking

    monkeypatch.delenv("RERANK_CONCURRENCY", raising=False)
    monkeypatch.setattr(reranking, "_predict_semaphore", None)
    _SlowCountingFake.active = 0
    _SlowCountingFake.max_active = 0
    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _SlowCountingFake())

    errors: list[BaseException] = []

    def ask() -> None:
        try:
            r = reranking.CrossEncoderReranker()
            r.rerank("q", [_chunk("a"), _chunk("b")], top_n=1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "a caller never returned -- deadlock"

    assert not errors, errors
    assert _SlowCountingFake.max_active >= 2, "predict() calls never overlapped at all"
    assert _SlowCountingFake.max_active <= 2, "more callers ran at once than the configured cap"


def test_concurrency_cap_is_configurable_and_actually_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RERANK_CONCURRENCY=1`` fully serializes ``predict()`` — proves the bound comes
    from the setting, not a hardcoded 2."""
    import threading

    import rag_app.reranking as reranking

    monkeypatch.setenv("RERANK_CONCURRENCY", "1")
    monkeypatch.setattr(reranking, "_predict_semaphore", None)
    _SlowCountingFake.active = 0
    _SlowCountingFake.max_active = 0
    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _SlowCountingFake())

    errors: list[BaseException] = []

    def ask() -> None:
        try:
            r = reranking.CrossEncoderReranker()
            r.rerank("q", [_chunk("a")], top_n=1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "a caller never returned -- deadlock"

    assert not errors, errors
    assert (
        _SlowCountingFake.max_active == 1
    ), "RERANK_CONCURRENCY=1 did not fully serialize predict()"


def test_concurrency_cap_does_not_corrupt_per_caller_timing_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller forced to wait behind the cap still gets its OWN correct
    ``rerank_inference`` duration (includes its queueing wait, same as a real request would
    experience it) — the shared semaphore must not pollute another thread's timing."""
    import threading
    import time

    import rag_app.reranking as reranking
    from rag_app import timing

    monkeypatch.setenv("RERANK_CONCURRENCY", "1")  # forces full serialization => real waits
    monkeypatch.setattr(reranking, "_predict_semaphore", None)
    _SlowCountingFake.sleep_s = 0.1
    _SlowCountingFake.active = 0
    _SlowCountingFake.max_active = 0
    monkeypatch.setattr(reranking, "load_cross_encoder", lambda *_a, **_k: _SlowCountingFake())

    recorded: dict[str, float] = {}
    lock = threading.Lock()
    errors: list[BaseException] = []

    def ask(label: str) -> None:
        try:
            r = reranking.CrossEncoderReranker()

            def on_stage(name: str, elapsed_s: float) -> None:
                if name == "rerank_inference":
                    with lock:
                        recorded[label] = elapsed_s

            with timing.recorder(streaming=False, on_stage=on_stage):
                r.rerank("q", [_chunk("a")], top_n=1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ask, args=(label,)) for label in ("first", "second")]
    # start "first" slightly before "second" so "second" is the one forced to queue
    threads[0].start()
    time.sleep(0.02)
    threads[1].start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    # the queued caller's own recorded duration includes its wait: with cap=1 and both
    # predict() calls sleeping 0.1s, "second" cannot finish before ~0.1s (its own wait) +
    # 0.1s (its own predict) have both elapsed.
    assert recorded["second"] >= 0.15, recorded
