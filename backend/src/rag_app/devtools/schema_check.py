"""``schema-check`` gate step: the tracked evaluation data files are well-formed.

Checks, without any stack:

- ``eval/golden_set.jsonl`` — one JSON object per line with exactly the keys the loader and the
  runner use; unique ids; answerable items name an ``expected_doc``, negative ones do not;
- ``eval/judge_labels.jsonl`` — the judge-validation labels (keys, unique ids, known buckets);
- ``eval/thresholds.json`` / ``eval/baseline_metrics.json`` — every gated metric present, a
  number in [0, 1];
- ``eval/judge_validation.json`` — ``cohen_kappa`` and ``threshold`` numbers.

When the (git-ignored) corpus ``data/chunks/chunks.jsonl`` exists, every ``expected_doc`` must
be a document of it; otherwise that cross-check is reported as skipped.

    python -m rag_app.devtools.schema_check [--root REPO]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

METRICS = ("retrieval_recall", "faithfulness", "correctness", "correct_abstention")
GOLDEN_KEYS = {"id", "question", "ground_truth", "expected_doc", "version", "answerable"}
LABEL_KEYS = {"id", "bucket", "question", "reference", "candidate", "label"}
LABEL_BUCKETS = {"found", "edge", "abstain"}


def _jsonl(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{number}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(row, dict):
            errors.append(f"{path.name}:{number}: not a JSON object")
            continue
        row["_line"] = number
        rows.append(row)
    return rows


def _unique_ids(name: str, rows: list[dict[str, Any]], errors: list[str]) -> None:
    seen: set[str] = set()
    for row in rows:
        ident = row.get("id")
        if not isinstance(ident, str) or not ident:
            errors.append(f"{name}:{row['_line']}: id must be a non-empty string")
        elif ident in seen:
            errors.append(f"{name}:{row['_line']}: duplicate id {ident!r}")
        else:
            seen.add(ident)


def check_golden(path: Path, corpus_docs: set[str] | None, errors: list[str]) -> int:
    rows = _jsonl(path, errors)
    if not rows:
        errors.append(f"{path.name}: empty")
    _unique_ids(path.name, rows, errors)
    for row in rows:
        where = f"{path.name}:{row['_line']}"
        keys = set(row) - {"_line"}
        if keys != GOLDEN_KEYS:
            missing, extra = GOLDEN_KEYS - keys, keys - GOLDEN_KEYS
            errors.append(
                f"{where}: keys differ (missing {sorted(missing)}, extra {sorted(extra)})"
            )
            continue
        for key in ("question", "ground_truth"):
            if not isinstance(row[key], str) or not row[key].strip():
                errors.append(f"{where}: {key} must be a non-empty string")
        if not isinstance(row["answerable"], bool):
            errors.append(f"{where}: answerable must be true/false")
        doc = row["expected_doc"]
        if row["answerable"] is True and not (isinstance(doc, str) and doc):
            errors.append(f"{where}: answerable item needs expected_doc")
        if row["answerable"] is False and doc is not None:
            errors.append(f"{where}: negative item must have expected_doc null")
        if isinstance(doc, str) and corpus_docs is not None and doc not in corpus_docs:
            errors.append(f"{where}: expected_doc {doc!r} is not a document of the corpus")
        if row["version"] is not None and not isinstance(row["version"], str):
            errors.append(f"{where}: version must be a string or null")
    return len(rows)


def check_labels(path: Path, errors: list[str]) -> int:
    rows = _jsonl(path, errors)
    _unique_ids(path.name, rows, errors)
    for row in rows:
        where = f"{path.name}:{row['_line']}"
        keys = set(row) - {"_line"}
        if keys != LABEL_KEYS:
            errors.append(f"{where}: keys differ from {sorted(LABEL_KEYS)}")
            continue
        if row["bucket"] not in LABEL_BUCKETS:
            errors.append(f"{where}: bucket must be one of {sorted(LABEL_BUCKETS)}")
        for key in ("question", "reference", "candidate", "label"):
            if not isinstance(row[key], str) or not row[key].strip():
                errors.append(f"{where}: {key} must be a non-empty string")
    return len(rows)


def _is_unit(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1


def check_metrics(path: Path, errors: list[str]) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path.name}: invalid JSON ({exc.msg})")
        return
    for key in METRICS:
        if not _is_unit(data.get(key)):
            errors.append(f"{path.name}: {key} must be a number in [0, 1]")


def check_judge_validation(path: Path, errors: list[str]) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path.name}: invalid JSON ({exc.msg})")
        return
    for key in ("cohen_kappa", "threshold"):
        value = data.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool):
            errors.append(f"{path.name}: {key} must be a number")


def corpus_documents(chunks: Path) -> set[str] | None:
    if not chunks.is_file():
        return None
    docs: set[str] = set()
    for line in chunks.read_text(encoding="utf-8").splitlines():
        if line.strip():
            docs.add(str(json.loads(line)["doc_slug"]))
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the evaluation data files.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--chunks", type=Path, help="corpus chunks (default: <root>/data/chunks/chunks.jsonl)"
    )
    args = parser.parse_args(argv)
    eval_dir = args.root / "eval"
    errors: list[str] = []

    docs = corpus_documents(args.chunks or args.root / "data" / "chunks" / "chunks.jsonl")
    n_golden = check_golden(eval_dir / "golden_set.jsonl", docs, errors)
    n_labels = check_labels(eval_dir / "judge_labels.jsonl", errors)
    check_metrics(eval_dir / "thresholds.json", errors)
    check_metrics(eval_dir / "baseline_metrics.json", errors)
    check_judge_validation(eval_dir / "judge_validation.json", errors)

    corpus = "skipped (no data/chunks/chunks.jsonl)" if docs is None else f"{len(docs)} documents"
    print(f"schema-check: golden set {n_golden} items, judge labels {n_labels}, corpus {corpus}")
    for error in errors:
        print(f"  FAIL {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
