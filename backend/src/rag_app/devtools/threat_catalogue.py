"""``threat-catalogue`` gate step (T12.0.5): every catalogue entry in
``docs/security/THREAT_MODEL.md`` has a phase and a test ID, and every entry marked
``mitigated`` points at a test that actually exists.

The catalogue lives as ordinary Markdown tables between two HTML comment markers::

    <!-- threat-catalogue:begin -->
    ...one or more "| ID | Entry | Status | Defence difficulty | Attack level | Phase |
    Test ID | Notes |" tables...
    <!-- threat-catalogue:end -->

``Status`` must be one of ``mitigated``, ``planned`` or ``accepted``. ``planned`` and
``accepted`` rows may name a future task (``T15.1.1``) or a plain tracking ID (``DEBT-020``)
in **Test ID** — there is nothing to run yet. A ``mitigated`` row's **Test ID** must be a
``path::test_function`` that resolves to a tracked file containing ``def test_function(``;
this is the regression test for a catalogue that drifts from the code it claims mitigates a
risk (an entry marked done when the test backing it was renamed or deleted). A data row whose
cell count doesn't match its table's header (too few cells, or extra ones from an unescaped
``|`` inside a cell) is a failure too, never a silent skip (DA-12aA-1) — a catalogue that quietly
shrinks must fail the gate, not just report fewer entries than it should.

    python -m rag_app.devtools.threat_catalogue [--root REPO] [FILE]

Exit code: 0 every entry valid, 1 at least one problem.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_BEGIN = "<!-- threat-catalogue:begin -->"
_END = "<!-- threat-catalogue:end -->"
_STATUSES = {"mitigated", "planned", "accepted"}
_MALFORMED = "malformed"  # sentinel status: never a real entry, always a CatalogueProblem
_DEFAULT_FILE = Path("docs/security/THREAT_MODEL.md")
_TEST_ID = re.compile(r"^(?P<path>[\w./-]+\.py)::(?P<func>[A-Za-z_][\w]*)$")


@dataclass(frozen=True)
class CatalogueRow:
    source: Path
    line: int
    entry_id: str
    status: str
    phase: str
    test_id: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.entry_id}"


@dataclass(frozen=True)
class CatalogueProblem:
    row: CatalogueRow
    reason: str

    def __str__(self) -> str:
        return f"{self.row.source}:{self.row.line}: {self.row.entry_id} — {self.reason}"


def _table_cells(line: str) -> list[str] | None:
    """A Markdown table row's cells, or ``None`` if ``line`` does not open with ``|``."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    return [c.strip() for c in stripped.strip("|").split("|")]


# Per-table state: 0 outside a table, 1 just saw the header row (next is the "|---|" rule),
# 2 in data rows (until a non-"|" line ends the table).
_OUTSIDE, _HEADER_SEEN, _IN_DATA = range(3)


def _malformed(source: Path, number: int, cells: list[str]) -> CatalogueRow:
    """A data row whose cell count doesn't match its table's header (too few cells, or
    extra ones from an unescaped ``|`` inside a cell) — flagged, never silently dropped
    (DA-12aA-1): a shrinking catalogue must fail the gate, not just print fewer entries."""
    label = cells[0] if cells and cells[0] else f"<malformed row, {len(cells)} cell(s)>"
    return CatalogueRow(
        source=source, line=number, entry_id=label, status=_MALFORMED, phase="", test_id=""
    )


def extract_rows(text: str, source: Path) -> list[CatalogueRow]:
    """Every data row of every table between the begin/end markers."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if _BEGIN in line)
        stop = next(i for i, line in enumerate(lines) if _END in line)
    except StopIteration:
        return []

    rows: list[CatalogueRow] = []
    state = _OUTSIDE
    expected_cols = 0
    for number, line in enumerate(lines[start + 1 : stop], start=start + 2):
        cells = _table_cells(line)
        if cells is None:
            state = _OUTSIDE
            continue
        if state == _OUTSIDE:
            expected_cols = len(cells)  # this line is the table's header row
            state = _HEADER_SEEN
            continue
        if state == _HEADER_SEEN:
            state = _IN_DATA  # this line is the "|---|---|" separator rule
            continue
        if len(cells) != expected_cols or len(cells) < 7 or not cells[0]:
            rows.append(_malformed(source, number, cells))
            continue
        entry_id, _entry, status, _difficulty, _attack, phase, test_id = cells[:7]
        rows.append(
            CatalogueRow(
                source=source,
                line=number,
                entry_id=entry_id,
                status=status.lower(),
                phase=phase,
                test_id=test_id,
            )
        )
    return rows


def _test_exists(root: Path, test_id: str) -> bool:
    match = _TEST_ID.match(test_id)
    if not match:
        return False
    path = root / match.group("path")
    if not path.is_file():
        return False
    pattern = re.compile(rf"^\s*def\s+{re.escape(match.group('func'))}\s*\(", re.MULTILINE)
    return bool(pattern.search(path.read_text(encoding="utf-8")))


def check(root: Path, rows: list[CatalogueRow]) -> list[CatalogueProblem]:
    problems: list[CatalogueProblem] = []
    for row in rows:
        if row.status == _MALFORMED:
            problems.append(
                CatalogueProblem(
                    row,
                    "malformed row — wrong column count (check for an unescaped '|' or a "
                    "missing cell)",
                )
            )
            continue
        if not row.phase:
            problems.append(CatalogueProblem(row, "missing phase"))
        if not row.test_id:
            problems.append(CatalogueProblem(row, "missing test ID"))
            continue
        if row.status not in _STATUSES:
            problems.append(
                CatalogueProblem(row, f"status '{row.status}' is not one of {sorted(_STATUSES)}")
            )
        if row.status == "mitigated" and not _test_exists(root, row.test_id):
            problems.append(
                CatalogueProblem(
                    row,
                    f"mitigated but test ID '{row.test_id}' does not resolve to an "
                    "existing test function",
                )
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every threat-catalogue entry has a phase and test ID; mitigated "
        "entries point at a real test (T12.0.5)."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("file", type=Path, nargs="?", default=None)
    args = parser.parse_args(argv)

    target = args.file if args.file is not None else (args.root / _DEFAULT_FILE)
    if not target.is_file():
        print(f"threat-catalogue: FAIL — {target} not found", file=sys.stderr)
        return 1

    rows = extract_rows(target.read_text(encoding="utf-8"), target)
    if not rows:
        print(f"threat-catalogue: FAIL — no catalogue rows found in {target}", file=sys.stderr)
        return 1

    problems = check(args.root, rows)
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
    print(f"threat-catalogue: {len(rows)} entries ({summary})")
    for problem in problems:
        print(f"  FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
