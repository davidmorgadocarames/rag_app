"""Cross-encoder reranking (bge-reranker).

Retrieval (vector + BM25) is fast but approximate; a cross-encoder re-scores each
(query, chunk) pair jointly for higher precision, at the cost of latency. The model
is heavy (torch), so it is imported lazily: importing this module stays cheap and the
pure ordering logic is unit-testable without loading torch.
"""

from __future__ import annotations

import argparse
import gc
import os
import re
import threading
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from rag_app import timing
from rag_app.config import get_settings
from rag_app.db.session import make_session_factory
from rag_app.retrieval import RetrievedChunk, hybrid_search

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

# The cross-encoder caps each (query, chunk) pair's tokenized length (T11.4.1): longer pairs
# are truncated instead of raising, and a fixed cap keeps CPU inference time bounded and
# predictable across requests regardless of how long a retrieved chunk happens to be. Mirrors
# Settings.reranker_max_length's default (T11.5.1, 11b block F) -- kept here too since this
# module must stay importable (and this constant usable) without constructing Settings.
RERANKER_MAX_LENGTH = 512


class _QuantizedCrossEncoder:
    """A ``predict(pairs) -> list[float]`` adapter around a raw, dynamically int8-quantized
    ``transformers`` model (T11.5.1, 11b block F quantization note).

    ``sentence_transformers.CrossEncoder``'s own dynamic-quantization integration is broken:
    reassigning a ``torch.quantization.quantize_dynamic(...)`` result back onto
    ``CrossEncoder.model`` corrupts the wrapper's forward-kwargs introspection (reproduced
    while measuring the T11.5.1 matrix -- a ``BatchEncoding`` ends up passed positionally as
    ``input_ids`` deep inside the HF model, raising ``AttributeError``). This bypasses the
    wrapper entirely instead: tokenize and call the quantized model directly, then apply the
    SAME sigmoid activation ``CrossEncoder.predict()`` uses for a single-logit (``num_labels
    == 1``) reranker model, so a score here means the same thing as a score from the normal
    (non-quantized) path -- ``order_by_scores`` and ``Settings.thin_threshold`` both only
    ever see a plain float, never which path produced it.
    """

    def __init__(self, tokenizer: Any, model: Any, max_length: int) -> None:
        self._tokenizer = tokenizer
        self._model = model
        self._max_length = max_length

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        import torch

        queries = [pair[0] for pair in pairs]
        texts = [pair[1] for pair in pairs]
        encoded = self._tokenizer(
            queries,
            texts,
            padding=True,
            truncation=True,
            max_length=self._max_length,
            return_tensors="pt",
        )
        with torch.no_grad():
            logits = self._model(**encoded).logits.squeeze(-1)
            scores = torch.sigmoid(logits)
        return [float(score) for score in scores.reshape(-1)]


def order_by_scores(
    candidates: list[RetrievedChunk], scores: list[float], top_n: int
) -> list[RetrievedChunk]:
    """Attach rerank scores, sort by them (descending), and keep the top ``top_n``."""
    if len(candidates) != len(scores):
        raise ValueError("candidates and scores must have the same length")
    for candidate, score in zip(candidates, scores, strict=True):
        candidate.score = score
    ranked = sorted(candidates, key=lambda chunk: chunk.score, reverse=True)
    return ranked[:top_n]


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TRUTHY = {"1", "true", "yes", "on"}


class RerankerModelError(RuntimeError):
    """The reranker model cannot be loaded at its pinned revision."""


def pinned_revision(model_name: str, revision: str | None) -> str:
    """The Hub commit to load: explicit, or the configured one for the configured model.

    Only a full commit hash is accepted — a branch or tag (``main``) is mutable and would let
    the Hub repo change what runs (DA-B-7, PHASE_TASKS row 37d).
    """
    settings = get_settings()
    if revision is None and model_name == settings.reranker_model:
        revision = settings.reranker_revision
    if revision is None or not _COMMIT.match(revision):
        raise RerankerModelError(
            f"reranker {model_name!r} needs a pinned revision (a 40-hex commit hash);"
            f" got {revision!r}"
        )
    return revision


def hub_offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in _TRUTHY


def _load_quantized_cross_encoder(
    model_name: str, revision: str, max_length: int
) -> _QuantizedCrossEncoder:
    """The ``quantize=True`` path of :func:`load_cross_encoder` (T11.5.1): same pinned-
    revision/offline/trust rules, but via raw ``transformers`` + a dynamic int8 quantization
    pass instead of ``sentence_transformers.CrossEncoder`` (see ``_QuantizedCrossEncoder``)."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    kwargs: dict[str, Any] = {"revision": revision, "trust_remote_code": False}
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True, **kwargs)
        model = AutoModelForSequenceClassification.from_pretrained(
            model_name, local_files_only=True, **kwargs
        )
    except (OSError, ValueError) as exc:  # not in the cache (or an incomplete snapshot)
        if hub_offline():
            raise RerankerModelError(
                f"reranker {model_name}@{revision[:12]} is not in the local cache and"
                " HF_HUB_OFFLINE is set"
            ) from exc
        tokenizer = AutoTokenizer.from_pretrained(model_name, **kwargs)
        model = AutoModelForSequenceClassification.from_pretrained(model_name, **kwargs)
    model.eval()
    # torch's own type stubs do not cover this function (DA-11bE-2 territory: torch's CPU/
    # quantization surface is only partially typed).
    quantized = torch.quantization.quantize_dynamic(  # type: ignore[attr-defined]
        model, {torch.nn.Linear}, dtype=torch.qint8
    )
    quantized.eval()
    # quantize_dynamic() builds the int8 copy while the original fp32 model is still alive,
    # so peak RSS during THIS call is fp32 + int8 size combined; dropping the fp32 reference
    # and collecting immediately (rather than waiting for the next GC cycle, which may be a
    # while on a long-running server process) lets the allocator reclaim it as early as
    # possible (RSS measured in the ADR, decision 8, 11b block F).
    del model
    gc.collect()
    return _QuantizedCrossEncoder(tokenizer, quantized, max_length)


def load_cross_encoder(
    model_name: str,
    revision: str,
    *,
    max_length: int = RERANKER_MAX_LENGTH,
    quantize: bool = False,
) -> CrossEncoder | _QuantizedCrossEncoder:
    """Load the cross-encoder at exactly ``revision``, never running Hub code.

    1. From the local Hugging Face cache (or a model baked into the image), offline
       (``local_files_only``): no network call at all once the snapshot is present.
    2. Only if it is not cached, and ``HF_HUB_OFFLINE`` is not set: download that same commit.
       With ``HF_HUB_OFFLINE=1`` a missing snapshot is an error, never a download.
    ``trust_remote_code`` is always False. ``quantize=True`` (T11.5.1) loads via
    :func:`_load_quantized_cross_encoder` instead -- a different object, but one with the
    SAME ``predict(pairs) -> list[float]`` interface, so every caller stays unchanged.
    """
    if quantize:
        return _load_quantized_cross_encoder(model_name, revision, max_length)

    from sentence_transformers import CrossEncoder

    options: dict[str, Any] = {
        "revision": revision,
        "trust_remote_code": False,
        "max_length": max_length,
    }
    model: CrossEncoder
    try:
        model = CrossEncoder(model_name, local_files_only=True, **options)
        return model
    except (OSError, ValueError) as exc:  # not in the cache (or an incomplete snapshot)
        if hub_offline():
            raise RerankerModelError(
                f"reranker {model_name}@{revision[:12]} is not in the local cache and"
                " HF_HUB_OFFLINE is set"
            ) from exc
    model = CrossEncoder(model_name, **options)
    return model


class CrossEncoderReranker:
    """Reranks retrieved chunks with a bge-reranker cross-encoder (pinned revision)."""

    def __init__(
        self,
        model_name: str | None = None,
        revision: str | None = None,
        *,
        max_length: int | None = None,
        quantize: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.reranker_model
        self.revision = pinned_revision(self.model_name, revision)
        # Both default to the production Settings values but accept an override (T11.5.1's
        # experiment matrix constructs several instances with different values in the SAME
        # process without touching the environment).
        self.max_length = max_length if max_length is not None else settings.reranker_max_length
        self.quantize = quantize if quantize is not None else settings.reranker_quantize
        self._model: CrossEncoder | _QuantizedCrossEncoder | None = None

    def _ensure_model(self) -> CrossEncoder | _QuantizedCrossEncoder:
        if self._model is None:
            # Measured separately from inference (T11.3.1): the first request after a cold
            # start pays this once (warm-up removes it from later requests, T11.4.1).
            with timing.stage("rerank_load"):
                self._model = load_cross_encoder(
                    self.model_name,
                    self.revision,
                    max_length=self.max_length,
                    quantize=self.quantize,
                )
        return self._model

    def rerank(
        self, query: str, candidates: list[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        """Re-score candidates against the query and return the best ``top_n``.

        ``model.predict()`` itself is bounded by the process-wide concurrency cap
        (T11.5.1b, DA-11bE-3): a caller beyond the cap waits here — never fails — until a
        slot frees up. The wait is measured as part of ``rerank_inference``: from a caller's
        point of view, queueing behind the same CPU bottleneck IS the cost of reranking
        under load, exactly what a real ``/chat`` request experiences; the in-flight
        chat-requests gauge (TF4) is unaffected, since a queued request is still genuinely
        in flight for the whole wait.
        """
        if not candidates:
            return []
        model = self._ensure_model()
        pairs = [(query, candidate.text) for candidate in candidates]
        with timing.stage("rerank_inference"), _get_predict_semaphore():
            scores = model.predict(pairs)
        return order_by_scores(candidates, [float(score) for score in scores], top_n)

    def warm_up(self) -> None:
        """Load the model and run one dummy ``predict`` (T11.4.1), so no real request ever
        pays the first-load/first-inference cost. Call once, from the API lifespan, before
        serving traffic — never per-request.

        ``_ensure_model()`` wraps the (real, one-time) load in ``timing.stage("rerank_load")``
        exactly as a normal request does; the dummy ``predict`` below wraps the same way in
        ``timing.stage("rerank_inference")``. Outside a ``timing.recorder()`` (none is open at
        startup) both are no-ops (generation.py's own docstring on ``answer_from_chunks``), so
        warm-up never pollutes a per-answer timing record; every REAL request afterwards finds
        ``self._model`` already set, so its own ``rerank_load`` is ~0 (T11.4.4 measures this).
        """
        model = self._ensure_model()
        with timing.stage("rerank_inference"):
            model.predict([("warm-up query", "warm-up passage")])


_shared_reranker: CrossEncoderReranker | None = None
_shared_reranker_lock = threading.Lock()

_predict_semaphore: threading.Semaphore | None = None
_predict_semaphore_lock = threading.Lock()


def _get_predict_semaphore() -> threading.Semaphore:
    """The process-wide bound on concurrent ``CrossEncoder.predict()`` calls (T11.5.1b).

    Built lazily from ``Settings.rerank_concurrency`` (default 2) the first time it is
    needed, then reused — same double-checked-locking shape as ``get_shared_reranker()``.
    Tests reset it the same way (``monkeypatch.setattr(reranking, "_predict_semaphore",
    None)``) to pick up a changed setting.
    """
    global _predict_semaphore
    semaphore = _predict_semaphore
    if semaphore is None:
        with _predict_semaphore_lock:
            semaphore = _predict_semaphore
            if semaphore is None:
                limit = max(1, get_settings().rerank_concurrency)
                semaphore = _predict_semaphore = threading.Semaphore(limit)
    return semaphore


def get_shared_reranker() -> CrossEncoderReranker:
    """The ONE process-wide reranker instance (T11.4.1): loaded at most once, however many
    requests ask for it. The API lifespan warms it up before serving traffic; every other
    caller (both generation paths, the eval runner, ``agentic.py``) gets the SAME instance
    instead of constructing its own — the ~1 s model construction and any first-call warm-up
    cost are paid once per process, not once per answer.

    Double-checked locking: cheap on the (overwhelmingly common) already-built path, and
    still correct if two requests race to build it on a cold process.
    """
    global _shared_reranker
    reranker = _shared_reranker
    if reranker is None:
        with _shared_reranker_lock:
            reranker = _shared_reranker
            if reranker is None:
                reranker = _shared_reranker = CrossEncoderReranker()
    return reranker


def retrieve(
    session: Session,
    query: str,
    *,
    reranker: CrossEncoderReranker,
    candidate_k: int | None = None,
    top_n: int | None = None,
    version: str | None = None,
) -> list[RetrievedChunk]:
    """Hybrid retrieve a candidate pool, then rerank down to ``top_n``."""
    settings = get_settings()
    candidate_k = candidate_k or settings.top_k
    top_n = top_n or settings.rerank_top_n
    candidates = hybrid_search(
        session, query, top_k=candidate_k, candidate_k=candidate_k, version=version
    )
    return reranker.rerank(query, candidates, top_n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query with hybrid retrieval + rerank.")
    parser.add_argument("query", help="the question to search for")
    parser.add_argument("--version", default=None, help="filter by corpus version")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--candidate-k", type=int, default=None)
    parser.add_argument("--model", default=None, help="override the reranker model")
    parser.add_argument(
        "--revision", default=None, help="Hub commit of --model (required with --model)"
    )
    args = parser.parse_args(argv)

    reranker = CrossEncoderReranker(model_name=args.model, revision=args.revision)
    session_factory = make_session_factory()
    with session_factory() as session:
        results = retrieve(
            session,
            args.query,
            reranker=reranker,
            candidate_k=args.candidate_k,
            top_n=args.top_n,
            version=args.version,
        )

    print(f'\nQuery: {args.query!r}  (reranked, version={args.version or "any"})\n')
    for i, chunk in enumerate(results, start=1):
        snippet = " ".join(chunk.text.split())[:160]
        print(f"{i}. [{chunk.version}] {chunk.heading[:60]}  (score={chunk.score:.4f})")
        print(f"   {snippet}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
