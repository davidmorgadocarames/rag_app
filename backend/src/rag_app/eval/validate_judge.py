"""Validate the LLM judge against a labeled set (agreement corrected for chance).

An LLM judge is only trustworthy if it agrees with known-correct labels. This runs the
judge over eval/judge_labels.jsonl (found / edge / abstain cases with objective labels)
and reports accuracy + Cohen's kappa, writing eval/judge_validation.json. Re-run whenever
the judge model or prompt changes. Needs Ollama; skips gracefully if it is not running.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx

from rag_app.config import get_settings
from rag_app.eval.dataset import REPO_ROOT
from rag_app.eval.judge import judge_correctness
from rag_app.eval.metrics import cohen_kappa
from rag_app.llm import OllamaChat

LABELS_PATH = REPO_ROOT / "eval" / "judge_labels.jsonl"
VALIDATION_PATH = REPO_ROOT / "eval" / "judge_validation.json"
_KAPPA_THRESHOLD = 0.6


@dataclass(frozen=True)
class JudgeLabel:
    id: str
    bucket: str
    question: str
    reference: str
    candidate: str
    label: bool  # True == the candidate should be judged CORRECT


def load_labels(path: Path = LABELS_PATH) -> list[JudgeLabel]:
    labels: list[JudgeLabel] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            labels.append(
                JudgeLabel(
                    id=row["id"],
                    bucket=row.get("bucket", "?"),
                    question=row["question"],
                    reference=row["reference"],
                    candidate=row["candidate"],
                    label=row["label"] == "correct",
                )
            )
    return labels


def _ollama_up() -> bool:
    settings = get_settings()
    try:
        httpx.get(f"{settings.ollama_host.rstrip('/')}/api/tags", timeout=5).raise_for_status()
    except Exception:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the correctness judge.")
    parser.add_argument("--labels", type=Path, default=LABELS_PATH)
    parser.add_argument("--threshold", type=float, default=_KAPPA_THRESHOLD)
    args = parser.parse_args(argv)

    if not _ollama_up():
        print("judge validation: SKIP (Ollama not reachable)")
        return 0

    labels = load_labels(args.labels)
    chat = OllamaChat()

    y_true: list[bool] = []
    y_pred: list[bool] = []
    bucket_totals: dict[str, int] = defaultdict(int)
    bucket_hits: dict[str, int] = defaultdict(int)
    tp = fp = tn = fn = 0

    for item in labels:
        pred = judge_correctness(chat, item.question, item.reference, item.candidate)
        y_true.append(item.label)
        y_pred.append(pred)
        bucket_totals[item.bucket] += 1
        if pred == item.label:
            bucket_hits[item.bucket] += 1
        if item.label and pred:
            tp += 1
        elif not item.label and pred:
            fp += 1
        elif not item.label and not pred:
            tn += 1
        else:
            fn += 1

    n = len(labels)
    accuracy = sum(1 for a, b in zip(y_true, y_pred, strict=True) if a == b) / n if n else 1.0
    kappa = cohen_kappa(y_true, y_pred)

    report = {
        "n": n,
        "accuracy": accuracy,
        "cohen_kappa": kappa,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "per_bucket_accuracy": {
            b: bucket_hits[b] / bucket_totals[b] for b in sorted(bucket_totals)
        },
        "threshold": args.threshold,
    }
    VALIDATION_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("judge validation:")
    print(f"  n={n}  accuracy={accuracy:.3f}  cohen_kappa={kappa:.3f}")
    print(f"  confusion tp={tp} fp={fp} tn={tn} fn={fn}")
    for bucket, acc in report["per_bucket_accuracy"].items():
        print(f"  bucket {bucket}: {acc:.3f}")

    if kappa < args.threshold:
        print(f"\nJUDGE VALIDATION: FAIL (kappa {kappa:.3f} < {args.threshold})")
        return 1
    print(f"\nJUDGE VALIDATION: PASS (kappa {kappa:.3f} >= {args.threshold})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
