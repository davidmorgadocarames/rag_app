"""``venv`` gate step: the interpreter running this module has exactly the pinned versions.

The gate checks a tree (the main tree, or a temporary worktree at the pushed SHA) with a
virtualenv. If that venv does not match the tree's ``backend/requirements*.txt`` pins, the
tests, mypy and ``pip-audit`` prove nothing about the commit (DA-B-4). Run it with the
venv's interpreter:

    "$VENV/bin/python" -m rag_app.devtools.venv_sync --root REPO

Every ``name[extras]==version`` line (following ``-r`` includes) must be installed at that
version; local version labels are not allowed to differ either. Exit 0 in sync, 1 drift.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from pathlib import Path

REQUIREMENTS = ("backend/requirements.txt", "backend/requirements-dev.txt")
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;#]+)")


def canonical(name: str) -> str:
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    """``{canonical name: version}`` from a requirements file and its ``-r`` includes."""
    seen = seen if seen is not None else set()
    path = path.resolve()
    if path in seen:
        return {}
    seen.add(path)
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            pins.update(read_pins(path.parent / line.split(None, 1)[1], seen))
            continue
        match = _PIN.match(line)
        if match:
            pins[canonical(match.group(1))] = match.group(3)
    return pins


def drift(pins: dict[str, str], installed: dict[str, str]) -> list[str]:
    """Human-readable mismatches between pins and installed versions."""
    problems = []
    for name, wanted in sorted(pins.items()):
        have = installed.get(name)
        if have is None:
            problems.append(f"{name}: pinned {wanted}, not installed")
        elif have != wanted:
            problems.append(f"{name}: pinned {wanted}, installed {have}")
    return problems


def installed_versions() -> dict[str, str]:
    return {
        canonical(dist.metadata["Name"]): dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the venv against the pinned deps.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    pins: dict[str, str] = {}
    for rel in REQUIREMENTS:
        path = args.root / rel
        if path.exists():
            pins.update(read_pins(path))
    problems = drift(pins, installed_versions())
    if problems:
        print(f"venv drift ({sys.prefix}) against {args.root}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"venv in sync: {len(pins)} pins ({sys.prefix})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
