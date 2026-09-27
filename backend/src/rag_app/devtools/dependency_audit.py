"""``dependency-audit`` gate step: known vulnerabilities fail unless explicitly excepted.

Sources:

- Python: ``pip-audit`` (pinned in ``requirements-dev.txt``) over the backend virtualenv —
  runtime and development packages as installed from the pinned requirements.
- Frontend: ``npm audit --omit=dev`` (production dependency tree of ``frontend/``).

Every finding must match an entry of ``audit-exceptions.toml`` (repo root) by package and
advisory id (the id or any alias, e.g. ``GHSA-…`` / ``CVE-…`` / ``PYSEC-…``). An entry has an
``advisory``, ``package``, ``ecosystem`` (``pypi`` | ``npm``), ``reason`` and an ``expires``
date; a **past expiry fails** even if the advisory is no longer reported, so exceptions
cannot be forgotten. Entries that match nothing are listed as stale (not a failure: the CI
and local environments can differ).

    python -m rag_app.devtools.dependency_audit [--root REPO]
        [--pip-json FILE] [--npm-json FILE] [--today YYYY-MM-DD]

``--pip-json`` / ``--npm-json`` read saved reports instead of running the tools (tests).
Exit code: 0 clean, 1 unexcepted finding / expired or invalid exception / tool failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXCEPTIONS_FILE = "audit-exceptions.toml"
_ECOSYSTEMS = ("pypi", "npm")
_REQUIRED = ("advisory", "package", "ecosystem", "reason", "expires")


@dataclass(frozen=True)
class Finding:
    ecosystem: str
    package: str
    version: str
    advisory: str
    aliases: frozenset[str] = field(default_factory=frozenset)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset({self.advisory, *self.aliases})


@dataclass(frozen=True)
class AuditException:
    advisory: str
    package: str
    ecosystem: str
    reason: str
    expires: dt.date

    def matches(self, finding: Finding) -> bool:
        return (
            finding.ecosystem == self.ecosystem
            and _norm(finding.package) == _norm(self.package)
            and self.advisory in finding.ids
        )


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def load_exceptions(path: Path) -> tuple[list[AuditException], list[str]]:
    """Parse the exceptions file; returns (entries, validation errors)."""
    if not path.exists():
        return [], []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: list[AuditException] = []
    errors: list[str] = []
    for index, raw in enumerate(data.get("exception", []), start=1):
        missing = [key for key in _REQUIRED if not raw.get(key)]
        if missing:
            errors.append(f"exception #{index}: missing {', '.join(missing)}")
            continue
        expires = raw["expires"]
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            errors.append(f"exception #{index}: expires must be a TOML date (YYYY-MM-DD)")
            continue
        if raw["ecosystem"] not in _ECOSYSTEMS:
            errors.append(f"exception #{index}: ecosystem must be one of {_ECOSYSTEMS}")
            continue
        entries.append(
            AuditException(
                advisory=str(raw["advisory"]),
                package=str(raw["package"]),
                ecosystem=str(raw["ecosystem"]),
                reason=str(raw["reason"]),
                expires=expires,
            )
        )
    return entries, errors


def parse_pip_audit(report: dict[str, Any]) -> list[Finding]:
    """Findings from ``pip-audit -f json``."""
    findings: set[Finding] = set()
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            findings.add(
                Finding(
                    ecosystem="pypi",
                    package=dep["name"],
                    version=dep.get("version", "?"),
                    advisory=vuln["id"],
                    aliases=frozenset(vuln.get("aliases", [])),
                )
            )
    return sorted(findings, key=lambda f: (f.package, f.advisory))


def parse_npm_audit(report: dict[str, Any]) -> list[Finding]:
    """Findings from ``npm audit --json`` (v2 report): advisories, not the packages that
    only depend on a vulnerable one (those ``via`` entries are plain strings)."""
    findings: set[Finding] = set()
    for vuln in report.get("vulnerabilities", {}).values():
        for via in vuln.get("via", []):
            if not isinstance(via, dict):
                continue
            url = str(via.get("url", ""))
            ghsa = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
            advisory = ghsa or f"npm-{via.get('source')}"
            aliases = {f"npm-{via.get('source')}"} - {advisory}
            findings.add(
                Finding(
                    ecosystem="npm",
                    package=str(via.get("name", vuln.get("name", "?"))),
                    version=str(via.get("range", "?")),
                    advisory=advisory,
                    aliases=frozenset(aliases),
                )
            )
    return sorted(findings, key=lambda f: (f.package, f.advisory))


def _run_json(cmd: list[str], cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    # Both tools exit non-zero when they find something; only unparsable output is an error.
    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        tail = (proc.stderr or proc.stdout).strip()[-500:]
        raise RuntimeError(f"{' '.join(cmd[:3])} failed (exit {proc.returncode}): {tail}") from exc
    return data


def evaluate(
    findings: list[Finding], exceptions: list[AuditException], today: dt.date
) -> tuple[list[str], list[str]]:
    """Return (failures, notes)."""
    failures: list[str] = []
    notes: list[str] = []
    for exc in exceptions:
        if exc.expires < today:
            failures.append(
                f"exception expired {exc.expires}: {exc.ecosystem} {exc.package} {exc.advisory}"
                f" ({exc.reason})"
            )
    used: set[AuditException] = set()
    for finding in findings:
        matched = [exc for exc in exceptions if exc.matches(finding)]
        if matched:
            used.update(matched)
            continue
        alias = f" (aliases {', '.join(sorted(finding.aliases))})" if finding.aliases else ""
        failures.append(
            f"{finding.ecosystem} {finding.package} {finding.version}: {finding.advisory}{alias}"
            " — fix it or add a dated entry to audit-exceptions.toml"
        )
    for exc in exceptions:
        if exc not in used:
            notes.append(f"stale exception (not reported here): {exc.package} {exc.advisory}")
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dependency audit with dated exceptions.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--pip-json", type=Path)
    parser.add_argument("--npm-json", type=Path)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    exceptions, errors = load_exceptions(root / EXCEPTIONS_FILE)
    findings: list[Finding] = []
    try:
        if args.pip_json:
            pip_report = json.loads(args.pip_json.read_text(encoding="utf-8"))
        else:
            pip_report = _run_json(
                [sys.executable, "-m", "pip_audit", "-f", "json", "--progress-spinner", "off"],
                root,
            )
        findings += parse_pip_audit(pip_report)
        for dep in pip_report.get("dependencies", []):
            if "skip_reason" in dep:
                print(f"dependency-audit: note: {dep['skip_reason']}")
        if args.npm_json:
            npm_report = json.loads(args.npm_json.read_text(encoding="utf-8"))
        else:
            npm_report = _run_json(["npm", "audit", "--omit=dev", "--json"], root / "frontend")
        findings += parse_npm_audit(npm_report)
    except RuntimeError as exc:
        errors.append(str(exc))

    failures, notes = evaluate(findings, exceptions, args.today)
    failures = errors + failures
    print(
        f"dependency-audit: {len(findings)} advisories found, {len(exceptions)} exceptions,"
        f" {len(failures)} failures"
    )
    for note in notes:
        print(f"  {note}")
    for failure in failures:
        print(f"  FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
