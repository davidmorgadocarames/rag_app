#!/usr/bin/env python3
"""Download the SecRAG OWASP corpus from official OWASP sources.

The corpus is deliberately *mini* and injection-focused, and it contains the same
topic across OWASP editions so the system can be tested on version/date drift:

    Injection ranking:  A1 (2017)  ->  A03 (2021)  ->  A05 (2025)

PDFs are downloaded to ``data/raw_pdfs/`` (they exercise the PDF -> Markdown
ingestion pipeline). Source Markdown is downloaded to ``data/raw_md/``. A
``data/corpus_manifest.json`` file records every item with its metadata
(version, effective_date, source URL) for later ingestion.

Usage:
    python scripts/fetch_corpus.py            # download everything (skip existing)
    python scripts/fetch_corpus.py --force    # re-download even if present
    python scripts/fetch_corpus.py --only md  # only markdown  (or: --only pdf)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
PDF_DIR = DATA_DIR / "raw_pdfs"
MD_DIR = DATA_DIR / "raw_md"
MANIFEST_PATH = DATA_DIR / "corpus_manifest.json"

USER_AGENT = (
    "SecRAG-corpus-fetcher/0.1 (+https://github.com/davidmorgadocarames/rag_app)"
)
TIMEOUT_SECONDS = 90

RAW = "https://raw.githubusercontent.com/OWASP"
TOP10 = f"{RAW}/Top10/master"
CHEATSHEETS = f"{RAW}/CheatSheetSeries/master/cheatsheets"


@dataclass(frozen=True)
class Source:
    """One corpus document and the metadata ingestion will need later."""

    slug: str
    url: str
    filename: str
    kind: str  # "pdf" | "markdown"
    title: str
    version: str
    effective_date: str | None  # ISO date if reliably known, else None
    category_rank: str | None = None  # e.g. "A03" for OWASP Top 10 categories
    notes: str = ""

    @property
    def dest(self) -> Path:
        return (PDF_DIR if self.kind == "pdf" else MD_DIR) / self.filename


# Every URL below was verified to return HTTP 200 from the official OWASP repos.
SOURCES: list[Source] = [
    # --- OWASP Top 10: Injection across editions (the version-drift showcase) ---
    Source(
        slug="top10-2021-a03-injection",
        url=f"{TOP10}/2021/docs/en/A03_2021-Injection.md",
        filename="owasp_top10_2021_A03_injection.md",
        kind="markdown",
        title="OWASP Top 10 2021 - A03: Injection",
        version="2021",
        effective_date="2021-09-24",
        category_rank="A03",
    ),
    Source(
        slug="top10-2025-a05-injection",
        url=f"{TOP10}/2025/docs/en/A05_2025-Injection.md",
        filename="owasp_top10_2025_A05_injection.md",
        kind="markdown",
        title="OWASP Top 10 2025 - A05: Injection",
        version="2025",
        effective_date=None,  # 2025 edition release date not pinned here
        category_rank="A05",
        notes="Injection dropped from A03 (2021) to A05 (2025).",
    ),
    # --- Broken Access Control across editions (breadth; #1 in both) ---
    Source(
        slug="top10-2021-a01-broken-access-control",
        url=f"{TOP10}/2021/docs/en/A01_2021-Broken_Access_Control.md",
        filename="owasp_top10_2021_A01_broken_access_control.md",
        kind="markdown",
        title="OWASP Top 10 2021 - A01: Broken Access Control",
        version="2021",
        effective_date="2021-09-24",
        category_rank="A01",
    ),
    Source(
        slug="top10-2025-a01-broken-access-control",
        url=f"{TOP10}/2025/docs/en/A01_2025-Broken_Access_Control.md",
        filename="owasp_top10_2025_A01_broken_access_control.md",
        kind="markdown",
        title="OWASP Top 10 2025 - A01: Broken Access Control",
        version="2025",
        effective_date=None,
        category_rank="A01",
    ),
    # --- Injection Cheat Sheets (defensive depth; continuously maintained) ---
    Source(
        slug="cs-sql-injection-prevention",
        url=f"{CHEATSHEETS}/SQL_Injection_Prevention_Cheat_Sheet.md",
        filename="cheatsheet_sql_injection_prevention.md",
        kind="markdown",
        title="OWASP Cheat Sheet - SQL Injection Prevention",
        version="current",
        effective_date=None,
    ),
    Source(
        slug="cs-os-command-injection-defense",
        url=f"{CHEATSHEETS}/OS_Command_Injection_Defense_Cheat_Sheet.md",
        filename="cheatsheet_os_command_injection_defense.md",
        kind="markdown",
        title="OWASP Cheat Sheet - OS Command Injection Defense",
        version="current",
        effective_date=None,
    ),
    Source(
        slug="cs-injection-prevention",
        url=f"{CHEATSHEETS}/Injection_Prevention_Cheat_Sheet.md",
        filename="cheatsheet_injection_prevention.md",
        kind="markdown",
        title="OWASP Cheat Sheet - Injection Prevention",
        version="current",
        effective_date=None,
    ),
    # --- Real PDFs (exercise the PDF -> Markdown ingestion pipeline) ---
    Source(
        slug="top10-2017-full-pdf",
        url=f"{TOP10}/2017/OWASP Top 10-2017 (en).pdf",
        filename="owasp_top10_2017_en.pdf",
        kind="pdf",
        title="OWASP Top 10 - 2017 (full)",
        version="2017",
        effective_date="2017-11-20",
        notes="Injection was A1 (#1) in 2017.",
    ),
    Source(
        slug="top10-2025-presentation-pdf",
        url=f"{TOP10}/2025/Presentations/OWASP Top Ten 2025.pdf",
        filename="owasp_top10_2025_presentation.pdf",
        kind="pdf",
        title="OWASP Top Ten 2025 (presentation)",
        version="2025",
        effective_date=None,
    ),
]


@dataclass
class Result:
    slug: str
    status: str  # "downloaded" | "skipped" | "failed"
    path: str
    bytes: int = 0
    error: str = ""


def _encode_url(url: str) -> str:
    """Percent-encode the path (handles spaces/parentheses in OWASP filenames)."""
    parts = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(parts.path)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
    )


def _download(url: str) -> bytes:
    request = urllib.request.Request(
        _encode_url(url), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read()


def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def fetch(source: Source, *, force: bool) -> Result:
    dest = source.dest
    if dest.exists() and not force:
        return Result(source.slug, "skipped", str(dest), dest.stat().st_size)

    try:
        data = _download(source.url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return Result(source.slug, "failed", str(dest), error=str(exc))

    if not data:
        return Result(source.slug, "failed", str(dest), error="empty response")
    if source.kind == "pdf" and not _looks_like_pdf(data):
        return Result(source.slug, "failed", str(dest), error="not a valid PDF")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return Result(source.slug, "downloaded", str(dest), len(data))


def write_manifest(sources: list[Source], results: list[Result]) -> None:
    by_key = {r.slug: r for r in results}
    items = []
    for source in sources:
        result = by_key.get(source.slug)
        entry = asdict(source)
        entry["downloaded_bytes"] = result.bytes if result else 0
        entry["status"] = result.status if result else "unknown"
        items.append(entry)
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps({"items": items}, indent=2), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the SecRAG OWASP corpus.")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if present"
    )
    parser.add_argument(
        "--only",
        choices=["pdf", "md"],
        default=None,
        help="restrict to one kind (pdf or md)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kind_filter = {"pdf": "pdf", "md": "markdown"}.get(args.only or "")
    sources = [s for s in SOURCES if not kind_filter or s.kind == kind_filter]

    results: list[Result] = [fetch(s, force=args.force) for s in sources]
    write_manifest(sources, results)

    print(f"\nSecRAG corpus fetch -> {DATA_DIR}\n")
    for result in results:
        icon = {"downloaded": "OK ", "skipped": "-- ", "failed": "!! "}[result.status]
        size = f"{result.bytes / 1024:6.1f} KB" if result.bytes else "        "
        line = f"  {icon} {result.status:11} {size}  {Path(result.path).name}"
        if result.error:
            line += f"  ({result.error})"
        print(line)

    failed = [r for r in results if r.status == "failed"]
    downloaded = sum(1 for r in results if r.status == "downloaded")
    skipped = sum(1 for r in results if r.status == "skipped")
    print(f"\n{downloaded} downloaded, {skipped} skipped, {len(failed)} failed.")
    print(f"Manifest: {MANIFEST_PATH}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
