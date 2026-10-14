"""``private-names`` gate step: no public, tracked Markdown file names a private planning
file (DEBT-026).

``PHASE_PLANNING.md``, ``PHASE_STATUS.md``, ``PHASE_TASKS.md``, ``DEBT_REGISTER.md``,
``HANDOFF.md``, ``ORCHESTRATION.md``, ``RUNBOOK_AZURE.md`` and ``ROADMAP.md`` are private
(git-excluded; ORCHESTRATION.md §1) — some live only on the orchestrator's machine. A public
doc that names one (even just "see PHASE_STATUS" or a path like ``status/phase-11a.md``) tells
a reader private planning material exists and roughly what it is about, for no benefit: the
*reasoning* behind a decision belongs in public docs, reworded in public terms; the private
file's name never needs to appear there (11b's ADR named eight of these before DEBT-026 fixed
it — this step is the regression test for that).

    python -m rag_app.devtools.private_names [--root REPO] [FILE ...]

Matches the bare identifiers as whole words (case-sensitive: these are all-caps tokens, not
ordinary prose) plus the two path-shaped names that are not safe as bare words on their own
(``ROADMAP.md`` — "our roadmap" is unremarkable prose; the literal filename is not — and any
``status/phase-...`` path). Exit code: 0 no hit, 1 at least one.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Private, git-excluded planning files (ORCHESTRATION.md §1) a public doc must never name.
_BARE_NAMES = (
    "DEBT_REGISTER",
    "PHASE_STATUS",
    "PHASE_PLANNING",
    "PHASE_TASKS",
    "HANDOFF",
    "ORCHESTRATION",
    "RUNBOOK_AZURE",
)
_PATTERNS = [re.compile(rf"\b{name}\b") for name in _BARE_NAMES] + [
    re.compile(r"\bROADMAP\.md\b"),
    re.compile(r"\bstatus/phase-"),
]


@dataclass(frozen=True)
class PrivateNameHit:
    source: Path
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.text.strip()}"


def check_file(path: Path) -> list[PrivateNameHit]:
    """Every line of ``path`` that names a private planning file."""
    hits: list[PrivateNameHit] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if any(pattern.search(line) for pattern in _PATTERNS):
            hits.append(PrivateNameHit(path, number, line))
    return hits


def tracked_markdown(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [root / name for name in out.split("\0") if name and (root / name).is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Public docs must never name a private planning file (DEBT-026)."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("files", nargs="*", type=Path, help="default: every tracked *.md")
    args = parser.parse_args(argv)

    files = args.files or tracked_markdown(args.root)
    hits: list[PrivateNameHit] = []
    for path in files:
        hits.extend(check_file(path))
    print(f"private-names: {len(files)} Markdown files checked")
    for hit in hits:
        print(f"  NAMED {hit}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
