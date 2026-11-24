"""Golden evaluation set loader.

Golden set v1 (`GoldenItem`/`load_golden_set` below) is untouched by T14.0.2 — the current
runner (`rag_app.eval.runner`) keeps reading `eval/golden_set.jsonl` through them exactly as
before. Golden set v2 (`Turn`/`Dialogue`/`load_dialogues`, added for T14.0.2/T14.0.3, phase 14,
DEBT-006/020/049) replays whole dialogues instead of single questions, so follow-ups, chit-chat
and router/memory behaviour can be measured alongside retrieval and generation. A **single-turn
item is just a one-turn dialogue**, so the v2 schema is a strict superset of v1: `load_dialogues`
accepts both the new `{"id", "turns": [...]}` shape and a bare v1 record (`{"id", "question",
...}`, no `"turns"` key) by wrapping the latter into a one-turn `Dialogue` on the fly. The
runner's migration to replay `Dialogue` objects (instead of calling `load_golden_set` directly)
is T14.0.4, block 14-A2 — this module only adds the schema + loader, nothing here is wired into
the pipeline yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_GOLDEN_SET = REPO_ROOT / "eval" / "golden_set.jsonl"
DEFAULT_GOLDEN_SET_V2 = REPO_ROOT / "eval" / "golden_set_v2.jsonl"

# Router intents a golden-set v2 turn can expect (T14.2.3's `{intent, ...}` shape; "greeting" is
# not a separate intent — a lone greeting is `chitchat`, fast-pathed by regex before the router).
ALLOWED_INTENTS = frozenset({"security", "chitchat", "followup", "ambiguous", "off_topic"})
ALLOWED_LANGUAGES = frozenset({"en", "es"})


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


@dataclass(frozen=True)
class Turn:
    """One user message inside a dialogue and what the pipeline is expected to do with it.

    `expected_doc`/`ground_truth` only apply to turns the pipeline is expected to retrieve/answer
    (`expected_intent in {"security", "followup"}`) and are `None` otherwise. `answerable` follows
    the same rule for turns that DO carry it, but a chit-chat/ambiguous/off-topic turn MAY also set
    it explicitly to `False` (as every `off_topic` item in `eval/golden_set_v2.jsonl` does, to say
    plainly "never give an uncited answer here") rather than leaving it `None` — both spellings are
    valid; no test or metric distinguishes them (DA-14-A1-2).
    """

    message: str
    expected_intent: str
    language: str
    expected_doc: str | None = None
    answerable: bool | None = None
    ground_truth: str | None = None

    def __post_init__(self) -> None:
        if self.expected_intent not in ALLOWED_INTENTS:
            raise ValueError(
                f"unknown expected_intent {self.expected_intent!r}; "
                f"must be one of {sorted(ALLOWED_INTENTS)}"
            )
        if self.language not in ALLOWED_LANGUAGES:
            raise ValueError(
                f"unknown language {self.language!r}; must be one of {sorted(ALLOWED_LANGUAGES)}"
            )
        if self.answerable and not self.ground_truth:
            raise ValueError("an answerable turn needs a ground_truth")


@dataclass(frozen=True)
class Dialogue:
    """A golden-set v2 item: one or more turns replayed in order with shared memory."""

    id: str
    turns: tuple[Turn, ...]

    def __post_init__(self) -> None:
        if not self.turns:
            raise ValueError(f"dialogue {self.id!r} has no turns")

    @property
    def is_single_turn(self) -> bool:
        return len(self.turns) == 1


def _turn_from_row(row: dict[str, Any]) -> Turn:
    return Turn(
        message=row["message"],
        expected_intent=row["expected_intent"],
        language=row["language"],
        expected_doc=row.get("expected_doc"),
        answerable=row.get("answerable"),
        ground_truth=row.get("ground_truth"),
    )


def _wrap_legacy_row(row: dict[str, Any]) -> Dialogue:
    """Wrap a v1 flat golden-set record (no "turns" key) into a one-turn `Dialogue`.

    Keeps v1 files (`eval/golden_set.jsonl`) loadable through the v2 loader unchanged, per
    T14.0.2 ("single-turn items = one-turn dialogues"). Every v1 item in this repo is an English
    security question, so the inferred `expected_intent`/`language` are fixed rather than guessed.
    """
    turn = Turn(
        message=row["question"],
        expected_intent="security",
        language="en",
        expected_doc=row.get("expected_doc"),
        answerable=bool(row["answerable"]),
        ground_truth=row.get("ground_truth"),
    )
    return Dialogue(id=row["id"], turns=(turn,))


def _dialogue_from_row(row: dict[str, Any]) -> Dialogue:
    if "turns" not in row:
        return _wrap_legacy_row(row)
    turns = tuple(_turn_from_row(t) for t in row["turns"])
    return Dialogue(id=row["id"], turns=turns)


def load_dialogues(path: Path = DEFAULT_GOLDEN_SET_V2) -> list[Dialogue]:
    """Load a golden-set v2 JSONL file (dialogue schema; v1-shaped rows are auto-wrapped)."""
    dialogues: list[Dialogue] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            dialogues.append(_dialogue_from_row(json.loads(line)))
    return dialogues
