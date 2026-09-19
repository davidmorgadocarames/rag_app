"""Benchmark: simple pipeline vs agentic router, on the golden set.

Runs both pipelines over the golden set, measuring the same quality metrics
(retrieval, faithfulness, correctness, abstention) plus wall-clock latency. The
router is only worth keeping if it improves quality without unacceptable latency.
The correctness judge is excluded from the latency (it is eval overhead, not user
latency).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from rag_app.config import get_settings
from rag_app.eval.dataset import DEFAULT_GOLDEN_SET, GoldenItem, load_golden_set
from rag_app.eval.judge import judge_correctness
from rag_app.eval.metrics import ItemResult, Metrics, compute_metrics
from rag_app.llm import OllamaChat

REPO_ROOT = Path(__file__).resolve().parents[4]
BENCHMARK_PATH = REPO_ROOT / "eval" / "benchmark.json"


@dataclass
class PipelineReport:
    name: str
    metrics: Metrics
    mean_latency_s: float
    max_latency_s: float
    rewrites: int


def _doc_slug(chunk_uid: str) -> str:
    return chunk_uid.rsplit("::", 1)[0]


def _judge(chat: OllamaChat, item: GoldenItem, answer_text: str, abstained: bool) -> bool | None:
    if not item.answerable:
        return None
    if abstained:
        return False
    return judge_correctness(chat, item.question, item.ground_truth, answer_text)


def run_pipeline(
    session: Session, items: list[GoldenItem], name: str, *, chat: OllamaChat, top_n: int
) -> PipelineReport:
    from rag_app.agentic import answer_agentic
    from rag_app.generation import answer_from_chunks
    from rag_app.reranking import CrossEncoderReranker, retrieve

    reranker = CrossEncoderReranker()
    results: list[ItemResult] = []
    latencies: list[float] = []
    rewrites = 0

    for item in items:
        start = time.perf_counter()
        if name == "agentic":
            routed = answer_agentic(
                session,
                item.question,
                chat=chat,
                reranker=reranker,
                top_n=top_n,
                version=item.version,
            )
            answer = routed.answer
            retrieved_docs = routed.retrieved_docs
            rewrites += int(routed.rewrote)
        else:
            chunks = retrieve(
                session, item.question, reranker=reranker, top_n=top_n, version=item.version
            )
            answer = answer_from_chunks(chat, item.question, chunks)
            retrieved_docs = [_doc_slug(c.chunk_uid) for c in chunks]
        latencies.append(time.perf_counter() - start)

        correct = _judge(chat, item, answer.text, answer.abstained)
        results.append(
            ItemResult(
                id=item.id,
                answerable=item.answerable,
                retrieved_docs=retrieved_docs,
                expected_doc=item.expected_doc,
                abstained=answer.abstained,
                grounded=answer.grounded,
                correct=correct,
            )
        )

    return PipelineReport(
        name=name,
        metrics=compute_metrics(results),
        mean_latency_s=statistics.mean(latencies),
        max_latency_s=max(latencies),
        rewrites=rewrites,
    )


def _print_table(reports: list[PipelineReport]) -> None:
    cols = ["correctness", "faithfulness", "retrieval_recall", "correct_abstention"]
    header = f"{'pipeline':10} " + " ".join(f"{c:>18}" for c in cols)
    header += f" {'mean_lat_s':>11} {'max_lat_s':>10} {'rewrites':>9}"
    print(header)
    for r in reports:
        m = r.metrics.as_dict()
        row = f"{r.name:10} " + " ".join(f"{m[c]:>18.3f}" for c in cols)
        row += f" {r.mean_latency_s:>11.2f} {r.max_latency_s:>10.2f} {r.rewrites:>9}"
        print(row)


def main(argv: list[str] | None = None) -> int:
    from rag_app.db.session import make_session_factory

    parser = argparse.ArgumentParser(description="Benchmark simple vs agentic RAG.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_SET)
    args = parser.parse_args(argv)

    settings = get_settings()
    chat = OllamaChat()
    items = load_golden_set(args.golden)

    reports: list[PipelineReport] = []
    with make_session_factory()() as session:
        for name in ("simple", "agentic"):
            reports.append(
                run_pipeline(session, items, name, chat=chat, top_n=settings.rerank_top_n)
            )

    print()
    _print_table(reports)
    print()

    BENCHMARK_PATH.write_text(
        json.dumps(
            {
                r.name: {
                    "metrics": r.metrics.as_dict(),
                    "mean_latency_s": r.mean_latency_s,
                    "max_latency_s": r.max_latency_s,
                    "rewrites": r.rewrites,
                }
                for r in reports
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
