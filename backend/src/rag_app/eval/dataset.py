"""Golden evaluation set loader."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_GOLDEN_SET = REPO_ROOT / "eval" / "golden_set.jsonl"


@dataclass(frozen=True)
class GoldenItem:
    id: str
    question: str
    ground_truth: str
    expected_doc: str | None
    version: str | None
    answerable: bool


def load_golden_set(path: Path = DEFAULT_GOLDEN_SET) -> list[GoldenItem]:
    """Load the golden set from a JSONL file."""
    items: list[GoldenItem] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            items.append(
                GoldenItem(
                    id=row["id"],
                    question=row["question"],
                    ground_truth=row["ground_truth"],
                    expected_doc=row.get("expected_doc"),
                    version=row.get("version"),
                    answerable=bool(row["answerable"]),
                )
            )
    return items
