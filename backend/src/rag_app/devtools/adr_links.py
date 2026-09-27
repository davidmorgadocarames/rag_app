"""``adr-links`` gate step: every relative Markdown link must resolve.

Checks every tracked ``*.md`` file (``git ls-files``) and, when ``RUNBOOK_PATH`` is set, the
private runbook kept outside the repository. A link is broken when its target file does not
exist, or when it points to an ``#anchor`` that no heading of the target produces (GitHub's
slug rules). External links (``http:``, ``https:``, ``mailto:``) are not fetched.

    python -m rag_app.devtools.adr_links [--root REPO] [FILE ...]

Exit code: 0 all links resolve, 1 at least one broken link. With ``RUNBOOK_PATH`` unset the
runbook check is an explicit SKIP (printed), never a silent pass.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# [text](target) — not images' alt text quirks, good enough for our docs. Inline code spans
# and fenced code blocks are removed before matching.
_LINK = re.compile(r"(?<!\!)\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_FENCE = re.compile(r"^(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_EXTERNAL = ("http://", "https://", "mailto:", "tel:", "ftp://")


@dataclass(frozen=True)
class BrokenLink:
    source: Path
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.target} — {self.reason}"


def github_slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, drop punctuation, spaces to hyphens."""
    text = re.sub(r"<[^>]+>", "", heading)  # inline HTML
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _strip_code(lines: list[str], *, spans: bool = True) -> list[str]:
    """Blank fenced code blocks; with ``spans`` also drop inline code spans."""
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line.lstrip()):
            in_fence = not in_fence
            out.append("")
            continue
        if in_fence:
            out.append("")
        else:
            out.append(_CODE_SPAN.sub("", line) if spans else line)
    return out


def anchors(path: Path) -> set[str]:
    """Every anchor the headings of ``path`` produce (duplicates get ``-1``, ``-2``…)."""
    seen: dict[str, int] = {}
    result: set[str] = set()
    # Code spans in a heading are part of its anchor text (only the backticks go).
    lines = _strip_code(path.read_text(encoding="utf-8").splitlines(), spans=False)
    for line in lines:
        match = _HEADING.match(line)
        if not match:
            continue
        slug = github_slug(match.group(1))
        count = seen.get(slug, 0)
        result.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return result


def check_file(path: Path) -> list[BrokenLink]:
    """Broken relative links in one Markdown file."""
    broken: list[BrokenLink] = []
    lines = _strip_code(path.read_text(encoding="utf-8").splitlines())
    for number, line in enumerate(lines, start=1):
        for match in _LINK.finditer(line):
            target = match.group(1)
            if target.startswith(_EXTERNAL):
                continue
            file_part, _, anchor = target.partition("#")
            dest = path if not file_part else (path.parent / file_part)
            if not dest.exists():
                broken.append(BrokenLink(path, number, target, "target does not exist"))
                continue
            if anchor and dest.is_file() and dest.suffix.lower() == ".md":
                if anchor.lower() not in anchors(dest):
                    broken.append(BrokenLink(path, number, target, "no such heading anchor"))
    return broken


def tracked_markdown(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [root / name for name in out.split("\0") if name and (root / name).is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check relative Markdown links.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("files", nargs="*", type=Path, help="default: every tracked *.md")
    args = parser.parse_args(argv)

    files = args.files or tracked_markdown(args.root)
    broken: list[BrokenLink] = []
    for path in files:
        broken.extend(check_file(path))
    print(f"adr-links: {len(files)} Markdown files checked")

    runbook = os.environ.get("RUNBOOK_PATH", "")
    if not runbook:
        print("adr-links: runbook SKIP (RUNBOOK_PATH not set)")
    elif not Path(runbook).is_file():
        broken.append(BrokenLink(Path(runbook), 0, runbook, "RUNBOOK_PATH is not a file"))
    else:
        found = check_file(Path(runbook))
        broken.extend(found)
        print(f"adr-links: runbook checked ({len(found)} broken)")

    for item in broken:
        print(f"  BROKEN {item}", file=sys.stderr)
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
