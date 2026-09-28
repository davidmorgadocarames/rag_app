"""Gate tooling: adr-links, dependency-audit, schema-check, gate-project isolation."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from rag_app.devtools import adr_links, dependency_audit, gate_compose, schema_check

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- adr-links ------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_github_slug_matches_github_rules() -> None:
    heading = "7. `containerapp update --set-env-vars` is not reliable for new variables"
    assert adr_links.github_slug(heading) == (
        "7-containerapp-update---set-env-vars-is-not-reliable-for-new-variables"
    )


def test_valid_links_and_anchors_pass(tmp_path: Path) -> None:
    _write(tmp_path / "docs/adr/adr_phase11_stability.md", "# ADR\n\n## Rollback plan\n")
    doc = _write(
        tmp_path / "README.md",
        "See [ADR](docs/adr/adr_phase11_stability.md#rollback-plan), [site](https://x.y),"
        " [self](#intro)\n\n## Intro\n\n`[code](nope.md)`\n\n```\n[fenced](nope.md)\n```\n",
    )
    assert adr_links.check_file(doc) == []


def test_a_broken_link_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _write(tmp_path / "README.md", "[old ADR](docs/adr/0004-azure.md)\n")
    broken = adr_links.check_file(doc)
    assert [b.target for b in broken] == ["docs/adr/0004-azure.md"]
    monkeypatch.delenv("RUNBOOK_PATH", raising=False)
    assert adr_links.main([str(doc)]) == 1


def test_a_missing_anchor_fails(tmp_path: Path) -> None:
    _write(tmp_path / "a.md", "# Title\n")
    doc = _write(tmp_path / "b.md", "[x](a.md#no-such-section)\n")
    assert [b.reason for b in adr_links.check_file(doc)] == ["no such heading anchor"]


def test_runbook_is_checked_when_runbook_path_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = _write(tmp_path / "ok.md", "# ok\n")
    runbook = _write(tmp_path / "private/RUNBOOK.md", "[env](../.env.example)\n")
    monkeypatch.delenv("RUNBOOK_PATH", raising=False)
    assert adr_links.main([str(ok)]) == 0
    assert "runbook SKIP (RUNBOOK_PATH not set)" in capsys.readouterr().out
    monkeypatch.setenv("RUNBOOK_PATH", str(runbook))
    assert adr_links.main([str(ok)]) == 1


def test_repository_links_resolve() -> None:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    broken = [
        b for path in adr_links.tracked_markdown(REPO_ROOT) for b in adr_links.check_file(path)
    ]
    assert broken == []


# --- dependency-audit -----------------------------------------------------------------

PIP_REPORT = {
    "dependencies": [
        {
            "name": "PyJWT",
            "version": "2.10.1",
            "vulns": [{"id": "PYSEC-2026-120", "aliases": ["GHSA-752w-5fwx-jx9f"]}],
        },
        {"name": "torch", "version": "2.14.0+cpu", "skip_reason": "not on PyPI"},
    ]
}
NPM_REPORT = {
    "vulnerabilities": {
        "next": {"name": "next", "via": ["postcss"]},
        "postcss": {
            "name": "postcss",
            "via": [
                {
                    "source": 1117015,
                    "name": "postcss",
                    "url": "https://github.com/advisories/GHSA-qx2v-qp2m-jg93",
                    "range": "<8.5.10",
                }
            ],
        },
    }
}
EXCEPTIONS = """
[[exception]]
advisory = "GHSA-qx2v-qp2m-jg93"
package = "postcss"
ecosystem = "npm"
reason = "inside next"
expires = 2027-06-30

[[exception]]
advisory = "GHSA-752w-5fwx-jx9f"  # alias of PYSEC-2026-120
package = "pyjwt"
ecosystem = "pypi"
reason = "interim"
expires = 2026-10-31
"""


OSV_CLEAN: dict[str, Any] = {"torch": {}}


def _audit(
    tmp_path: Path,
    exceptions: str,
    today: str = "2026-09-27",
    pip_report: dict[str, Any] | None = None,
    osv: dict[str, Any] | None = None,
) -> int:
    (tmp_path / "audit-exceptions.toml").write_text(exceptions, encoding="utf-8")
    pip_json = tmp_path / "pip.json"
    npm_json = tmp_path / "npm.json"
    osv_json = tmp_path / "osv.json"
    pip_json.write_text(json.dumps(pip_report or PIP_REPORT), encoding="utf-8")
    npm_json.write_text(json.dumps(NPM_REPORT), encoding="utf-8")
    osv_json.write_text(json.dumps(OSV_CLEAN if osv is None else osv), encoding="utf-8")
    return dependency_audit.main(
        [
            "--root",
            str(tmp_path),
            "--pip-json",
            str(pip_json),
            "--npm-json",
            str(npm_json),
            "--osv-json",
            str(osv_json),
            "--today",
            today,
        ]
    )


def test_npm_findings_are_advisories_not_dependents() -> None:
    findings = dependency_audit.parse_npm_audit(NPM_REPORT)
    assert [(f.package, f.advisory) for f in findings] == [("postcss", "GHSA-qx2v-qp2m-jg93")]


def test_audit_passes_when_every_finding_is_excepted(tmp_path: Path) -> None:
    assert _audit(tmp_path, EXCEPTIONS) == 0


def test_removing_an_entry_fails(tmp_path: Path) -> None:
    without_postcss = EXCEPTIONS.split("[[exception]]", 2)
    assert _audit(tmp_path, "[[exception]]" + without_postcss[2]) == 1


def test_a_past_expiry_fails(tmp_path: Path) -> None:
    assert _audit(tmp_path, EXCEPTIONS, today="2026-11-01") == 1


def test_an_invalid_entry_fails(tmp_path: Path) -> None:
    broken = EXCEPTIONS.replace('reason = "interim"\n', "")
    assert _audit(tmp_path, broken) == 1


def test_a_duplicated_entry_is_invalid(tmp_path: Path) -> None:
    duplicated = EXCEPTIONS + "[[exception]]" + EXCEPTIONS.split("[[exception]]", 2)[2]
    entries, errors = dependency_audit.load_exceptions(
        _write(tmp_path / "audit-exceptions.toml", duplicated)
    )
    assert len(entries) == 2
    assert errors and "duplicate" in errors[0]
    assert _audit(tmp_path, duplicated) == 1


def test_a_local_version_is_audited_by_its_base_version_via_osv(tmp_path: Path) -> None:
    vulnerable = {"torch": {"vulns": [{"id": "PYSEC-2099-1", "aliases": ["CVE-2099-1"]}]}}
    assert _audit(tmp_path, EXCEPTIONS, osv=vulnerable) == 1
    excepted = EXCEPTIONS + (
        '\n[[exception]]\nadvisory = "CVE-2099-1"\npackage = "torch"\necosystem = "pypi"\n'
        'reason = "test"\nexpires = 2027-01-01\n'
    )
    assert _audit(tmp_path, excepted, osv=vulnerable) == 0


def test_osv_is_queried_with_the_base_version() -> None:
    calls: list[tuple[str, str]] = []

    def fake(package: str, version: str) -> dict[str, Any]:
        calls.append((package, version))
        return {}

    findings, notes, errors = dependency_audit.audit_skipped([("torch", "2.14.0+cpu")], fake)
    assert calls == [("torch", "2.14.0")]
    assert (findings, errors) == ([], [])
    assert "audited as torch==2.14.0 via OSV" in notes[0]


def test_an_unauditable_package_fails(tmp_path: Path) -> None:
    report = {"dependencies": [{"name": "private-pkg", "version": "1.0", "skip_reason": "x"}]}
    assert _audit(tmp_path, EXCEPTIONS.split("[[exception]]", 2)[0], pip_report=report) == 1


def test_an_osv_failure_fails_the_audit() -> None:
    def broken(package: str, version: str) -> dict[str, Any]:
        raise RuntimeError("OSV query failed")

    _, _, errors = dependency_audit.audit_skipped([("torch", "2.14.0+cpu")], broken)
    assert errors == ["OSV query failed"]


def test_the_repository_exceptions_file_is_valid() -> None:
    entries, errors = dependency_audit.load_exceptions(REPO_ROOT / "audit-exceptions.toml")
    assert errors == []
    postcss = [e for e in entries if e.package == "postcss"]
    assert postcss and all(e.ecosystem == "npm" for e in postcss)
    assert all(e.expires > dt.date(2026, 9, 27) for e in entries)
    # D-6 a1: these were bumped; no exception may come back for them silently.
    bumped = {"cryptography", "pyjwt", "pytest", "setuptools"}
    assert not [e for e in entries if e.package.lower() in bumped]
    # DA-B-7: every entry states its class and a reason longer than a boilerplate line.
    python = [e for e in entries if e.ecosystem == "pypi"]
    assert all(e.reason.startswith(("INTERIM", "PERMANENT")) for e in python)
    assert all("reachab" in e.reason.lower() for e in python)


# --- schema-check ---------------------------------------------------------------------


def test_repository_eval_files_pass_schema_check() -> None:
    assert schema_check.main(["--root", str(REPO_ROOT)]) == 0


def test_schema_check_rejects_a_bad_golden_item(tmp_path: Path) -> None:
    shutil.copytree(REPO_ROOT / "eval", tmp_path / "eval")
    golden = tmp_path / "eval" / "golden_set.jsonl"
    rows = [json.loads(line) for line in golden.read_text().splitlines() if line.strip()]
    rows[0]["expected_doc"] = None  # answerable without a document
    rows.append(dict(rows[1]))  # duplicate id
    golden.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert schema_check.main(["--root", str(tmp_path)]) == 1


def test_schema_check_rejects_an_unknown_corpus_document(tmp_path: Path) -> None:
    shutil.copytree(REPO_ROOT / "eval", tmp_path / "eval")
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text(json.dumps({"doc_slug": "only-this-doc"}) + "\n", encoding="utf-8")
    assert schema_check.main(["--root", str(tmp_path), "--chunks", str(chunks)]) == 1


# --- gate project isolation -----------------------------------------------------------


def _resolved_gate_config() -> dict[str, Any]:
    """compose.gate.yml in the shape ``docker compose config --format json`` prints."""
    raw = yaml.safe_load((REPO_ROOT / "compose.gate.yml").read_text(encoding="utf-8"))
    for svc in raw["services"].values():
        ports = []
        for spec in svc.get("ports", []):
            spec = re.sub(r"\$\{[A-Z_]+:-(\d+)\}", r"\1", spec)  # the compose default value
            host_ip, published, target = spec.split(":")
            ports.append({"host_ip": host_ip, "published": published, "target": int(target)})
        svc["ports"] = ports
        svc["volumes"] = [
            {"type": "volume", "source": v.split(":")[0], "target": v.split(":")[1]}
            for v in svc.get("volumes", [])
        ]
    return raw


def test_gate_project_is_isolated_from_the_dev_stack() -> None:
    config = _resolved_gate_config()
    assert gate_compose.problems(config) == []
    text = (REPO_ROOT / "compose.gate.yml").read_text(encoding="utf-8")
    for dev in ("rag_ia_pgdata", "rag_app_pgdata", " pgdata:/", '"5432:5432"'):
        assert dev not in text


def test_gate_guard_rejects_dev_volumes_and_ports() -> None:
    config = _resolved_gate_config()
    config["name"] = "rag_ia"
    config["volumes"]["gate_pgdata"] = {"name": "rag_ia_pgdata", "external": True}
    config["services"]["db"]["ports"] = [{"host_ip": "", "published": "5432", "target": 5432}]
    config["services"]["db"]["volumes"].append(
        {"type": "bind", "source": "/var/lib/docker/volumes", "target": "/x"}
    )
    found = gate_compose.problems(config)
    assert len(found) == 6, found
