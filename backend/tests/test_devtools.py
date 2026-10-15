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

from rag_app.devtools import (
    adr_links,
    dependency_audit,
    gate_compose,
    private_names,
    schema_check,
    threat_catalogue,
)

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


# --- private-names (DEBT-026) ----------------------------------------------------------


def test_a_public_doc_naming_a_private_file_fails(tmp_path: Path) -> None:
    """Reproduces DEBT-026: the pre-fix ADR text would have failed exactly like this."""
    doc = _write(
        tmp_path / "docs/adr/example.md",
        'See PHASE_STATUS "Block D results" and RUNBOOK_AZURE.md §4 for the rest.\n',
    )
    hits = private_names.check_file(doc)
    assert [h.line for h in hits] == [1]
    assert private_names.main([str(doc)]) == 1


def test_a_status_archive_path_fails_even_without_the_bare_word(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "doc.md", "The verbatim history moved to status/phase-11a.md at phase close.\n"
    )
    assert private_names.check_file(doc) != []


def test_reworded_public_text_passes(tmp_path: Path) -> None:
    """Rewording keeps the reasoning, drops only the private file's literal name (the fix
    applied to docs/adr/adr_phase11_stability.md and docs/DEFINITION_OF_DONE.md)."""
    doc = _write(
        tmp_path / "doc.md",
        "See the internal block-D results notes and the internal Azure runbook §4.\n"
        "Our product roadmap is public; ROADMAP is just a word here.\n",
    )
    assert private_names.check_file(doc) == []


def test_repository_docs_never_name_a_private_file() -> None:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    hits = [
        h
        for path in private_names.tracked_markdown(REPO_ROOT)
        for h in private_names.check_file(path)
    ]
    assert hits == []


# --- threat-catalogue (T12.0.5) ---------------------------------------------------------

_CATALOGUE_HEADER = (
    "| ID | Entry | Status | Defence difficulty | Attack level | Phase | Test ID | Notes |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _catalogue_doc(tmp_path: Path, *rows: str) -> Path:
    body = "# Threat model\n\n<!-- threat-catalogue:begin -->\n\n" + _CATALOGUE_HEADER
    body += "".join(rows)
    body += "\n<!-- threat-catalogue:end -->\n"
    return _write(tmp_path / "THREAT_MODEL.md", body)


def test_extract_rows_reads_a_table_between_the_markers(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | planned | low | low | 15.1 | T15.1.1 | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    assert [r.entry_id for r in rows] == ["X-1"]
    assert rows[0].status == "planned"
    assert rows[0].phase == "15.1"
    assert rows[0].test_id == "T15.1.1"


def test_rows_outside_the_markers_are_ignored(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "doc.md",
        "| ID | Entry | Status | Defence difficulty | Attack level | Phase | Test ID | Notes |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| X-1 | Outside | planned | low | low | 1 | T1 | n/a |\n",
    )
    assert threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc) == []


def test_a_row_missing_a_phase_fails(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | planned | low | low |  | T15.1.1 | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    problems = threat_catalogue.check(tmp_path, rows)
    assert any("missing phase" in str(p) for p in problems)


def test_a_row_missing_a_test_id_fails(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | planned | low | low | 15.1 |  | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    problems = threat_catalogue.check(tmp_path, rows)
    assert any("missing test ID" in str(p) for p in problems)


def test_an_unknown_status_fails(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | someday | low | low | 15.1 | T15.1.1 | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    problems = threat_catalogue.check(tmp_path, rows)
    assert any("not one of" in str(p) for p in problems)


def test_a_planned_entry_with_a_future_task_id_passes(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | planned | low | low | 15.1 | T15.1.1 | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    assert threat_catalogue.check(tmp_path, rows) == []


def test_an_accepted_entry_with_a_debt_id_passes(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | accepted | n/a | n/a | accepted | DEBT-020 | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    assert threat_catalogue.check(tmp_path, rows) == []


def test_a_mitigated_entry_pointing_at_a_real_test_passes(tmp_path: Path) -> None:
    _write(tmp_path / "backend/tests/test_example.py", "def test_real_one() -> None:\n    pass\n")
    test_id = "backend/tests/test_example.py::test_real_one"
    doc = _catalogue_doc(
        tmp_path,
        f"| X-1 | Example | mitigated | low | low | 1 | {test_id} | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    assert threat_catalogue.check(tmp_path, rows) == []


def test_a_mitigated_entry_pointing_at_a_missing_test_fails(tmp_path: Path) -> None:
    """A catalogue entry claiming 'mitigated' must point at a test that actually exists —
    this reproduces a catalogue that drifted from a renamed/deleted test (regression guard
    for T12.0.5's Done-when: an entry without a real test fails)."""
    _write(tmp_path / "backend/tests/test_example.py", "def test_real_one() -> None:\n    pass\n")
    test_id = "backend/tests/test_example.py::test_renamed_away"
    doc = _catalogue_doc(
        tmp_path,
        f"| X-1 | Example | mitigated | low | low | 1 | {test_id} | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    problems = threat_catalogue.check(tmp_path, rows)
    assert any("does not resolve" in str(p) for p in problems)


def test_a_mitigated_entry_pointing_at_a_non_test_id_fails(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | mitigated | low | low | 1 | T1.2.3 | note |\n",
    )
    rows = threat_catalogue.extract_rows(doc.read_text(encoding="utf-8"), doc)
    problems = threat_catalogue.check(tmp_path, rows)
    assert any("does not resolve" in str(p) for p in problems)


def test_main_fails_without_any_catalogue_rows(tmp_path: Path) -> None:
    doc = _write(tmp_path / "empty.md", "# nothing here\n")
    assert threat_catalogue.main([str(doc)]) == 1


def test_main_passes_on_a_well_formed_catalogue(tmp_path: Path) -> None:
    doc = _catalogue_doc(
        tmp_path,
        "| X-1 | Example | planned | low | low | 15.1 | T15.1.1 | note |\n",
    )
    assert threat_catalogue.main([str(doc)]) == 0


def test_the_repository_catalogue_is_valid() -> None:
    """The real docs/security/THREAT_MODEL.md: every entry well-formed, every mitigated
    entry's test ID resolving to a real test — this is the gate step's own regression test
    against the live catalogue, not just the parser."""
    target = REPO_ROOT / "docs" / "security" / "THREAT_MODEL.md"
    rows = threat_catalogue.extract_rows(target.read_text(encoding="utf-8"), target)
    assert rows, "expected at least one catalogue row in THREAT_MODEL.md"
    assert threat_catalogue.check(REPO_ROOT, rows) == []


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
    # D-6 a1 + a2 (row 37d): these were bumped; no exception may come back for them silently.
    bumped = {"cryptography", "pyjwt", "pytest", "setuptools"}
    bumped |= {"fastapi", "starlette", "sentence-transformers", "transformers"}
    assert not [e for e in entries if e.package.lower() in bumped]
    # Row 37d: no INTERIM entry is left; only PERMANENT ones with a reachability reason.
    assert not [e for e in entries if e.reason.startswith("INTERIM")]
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
