"""Unit tests for the defence-lab probe framework (T13.3.2/T13.3.3/T13.3.4).

No live stack needed: the catalogue-building tests read the real
``docs/security/THREAT_MODEL.md`` (same file the ``threat-catalogue`` gate step already
validates); the subprocess-batching test spawns a REAL (throwaway, tiny) pytest run against
a fixture file that needs no database at all, proving the per-probe result parsing against
real pytest output rather than a hand-typed fixture string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rag_app.defence_lab import (
    CATALOGUE_PROBE_IDS,
    CatalogueProbeError,
    Probe,
    ProbeOutcome,
    ProbeResult,
    catalogue_probes,
    record_results,
    run_catalogue_probes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_catalogue_probes_resolves_every_curated_id_to_a_mitigated_row() -> None:
    probes = catalogue_probes()
    assert len(probes) == len(CATALOGUE_PROBE_IDS)
    for probe in probes:
        assert probe.id in CATALOGUE_PROBE_IDS
        assert "::" in probe.test_id
        assert probe.test_id.startswith("backend/")


def test_catalogue_probes_raises_for_an_unknown_id() -> None:
    with pytest.raises(CatalogueProbeError, match="NOT-A-REAL-ID"):
        catalogue_probes(["NOT-A-REAL-ID"])


def test_catalogue_probes_raises_for_a_non_mitigated_id(tmp_path: Path) -> None:
    catalogue = tmp_path / "THREAT_MODEL.md"
    catalogue.write_text(
        "<!-- threat-catalogue:begin -->\n"
        "| ID | Entry | Status | Defence difficulty | Attack level | Phase | Test ID | Notes |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| X1 | a planned control | planned | low | low | 99 | T99.1.1 | - |\n"
        "<!-- threat-catalogue:end -->\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogueProbeError, match="not 'mitigated'"):
        catalogue_probes(["X1"], catalogue=catalogue)


def test_run_catalogue_probes_without_a_target_fails_closed_never_silently_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    probe = Probe(id="X", expected_outcome="e", test_id="backend/tests/test_x.py::test_y")
    results = run_catalogue_probes([probe])
    assert len(results) == 1
    assert results[0].outcome.passed is False
    assert "TEST_DATABASE_URL" in results[0].outcome.evidence


def test_run_catalogue_probes_with_no_probes_is_a_no_op() -> None:
    assert run_catalogue_probes([]) == []


def test_run_catalogue_probes_parses_real_pytest_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real (tiny, DB-free) pytest subprocess run -- proves the per-probe result parsing
    against genuine pytest ``-v`` output, not a hand-typed fixture string."""
    # A validly-SHAPED (loopback, non-5432, not "rag") URL -- conftest.py's `pytest_configure`
    # eagerly validates `TEST_DATABASE_URL`'s shape whenever it is set, before collection
    # even runs, regardless of which tests get selected; nothing here ever connects to it
    # (the two fixture tests below use no DB fixture at all).
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:19999/postgres")
    fixture = REPO_ROOT / "backend" / "tests" / "_tmp_defence_lab_probe_fixture.py"
    fixture.write_text(
        "import pytest\n\n\n"
        "def test_passes() -> None:\n"
        "    assert True\n"
        "\n\n"
        "def test_fails() -> None:\n"
        "    assert False\n"
        "\n\n"
        '@pytest.mark.parametrize("n", [1, 2, 3])\n'
        "def test_parametrized_all_pass(n: int) -> None:\n"
        "    assert n > 0\n"
        "\n\n"
        '@pytest.mark.parametrize("n", [1, -1])\n'
        "def test_parametrized_one_fails(n: int) -> None:\n"
        "    assert n > 0\n",
        encoding="utf-8",
    )
    try:
        passing = Probe(
            id="P-OK",
            expected_outcome="passes",
            test_id="backend/tests/_tmp_defence_lab_probe_fixture.py::test_passes",
        )
        failing = Probe(
            id="P-FAIL",
            expected_outcome="passes",
            test_id="backend/tests/_tmp_defence_lab_probe_fixture.py::test_fails",
        )
        # REC-09's real catalogue test is parametrized -- selecting it by its bare node id
        # runs every variant (`nodeid[param] PASSED`, never the bare nodeid alone); found
        # running `defence-suite` for real against the live catalogue.
        parametrized_ok = Probe(
            id="P-PARAM-OK",
            expected_outcome="passes",
            test_id="backend/tests/_tmp_defence_lab_probe_fixture.py::test_parametrized_all_pass",
        )
        parametrized_mixed = Probe(
            id="P-PARAM-MIXED",
            expected_outcome="passes",
            test_id="backend/tests/_tmp_defence_lab_probe_fixture.py::test_parametrized_one_fails",
        )
        results = run_catalogue_probes(
            [passing, failing, parametrized_ok, parametrized_mixed], root=REPO_ROOT
        )
    finally:
        fixture.unlink(missing_ok=True)

    by_id = {r.probe.id: r for r in results}
    assert by_id["P-OK"].outcome.passed is True
    assert by_id["P-PARAM-OK"].outcome.passed is True
    assert by_id["P-PARAM-OK"].outcome.evidence.count("PASSED") == 3
    # One failing variant among several must count the WHOLE control as not matching --
    # never averaged/partial credit.
    assert by_id["P-PARAM-MIXED"].outcome.passed is False
    assert by_id["P-OK"].outcome.observed == "passed"
    assert by_id["P-FAIL"].outcome.passed is False
    assert by_id["P-FAIL"].outcome.observed == "failed"


class _FakeSession:
    """Captures `.add()` calls -- the ORM write itself is covered by `db-tests`
    (`test_startup_db.py`'s 0008 round-trip already proves the `defence_runs` table shape);
    this test only proves `record_results` builds the right row per result."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_record_results_builds_one_row_per_result_with_the_given_sha() -> None:
    session = _FakeSession()
    probe = Probe(
        id="OWASP-A05", expected_outcome="mitigated: ... passes", test_id="backend/x.py::y"
    )
    result = ProbeResult(probe, ProbeOutcome("passed", "backend/x.py::y: PASSED", True))

    record_results(session, [result], sha="deadbeef")  # type: ignore[arg-type]

    assert len(session.added) == 1
    row = session.added[0]
    assert row.probe_id == "OWASP-A05"
    assert row.git_sha == "deadbeef"
    assert row.passed is True
    assert row.evidence == "backend/x.py::y: PASSED"
