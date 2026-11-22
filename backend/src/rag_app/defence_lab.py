"""Defence-lab probe framework (T13.3.2/T13.3.3/T13.3.4, 13-D, local only -- DEC-028).

Every probe is keyed by a real ``docs/security/THREAT_MODEL.md`` catalogue id. Rather than
duplicating the assertion a security-suite test already makes (one implementation,
``status/phase-13-changes-since-plan.md`` item A2), a "catalogue probe" re-runs that EXACT
test -- ``rag_app.devtools.threat_catalogue.extract_rows`` (the SAME parser the
``threat-catalogue`` gate step uses, never a second drifting reader) resolves the catalogue
entry's own **Test ID** cell to a ``path::test_function``, and this module drives every probe
of one run through a SINGLE batched ``pytest -v`` subprocess (one process total, not one per
probe -- most of pytest's own cost is interpreter start-up/collection, not the test body).

**Fixed local target (T13.3.2).** The catalogue probes need the same local test-DB harness
every other ``db``-marked test uses (``backend/tests/db_harness.py``): ``TEST_DATABASE_URL``
pointing at a local, non-5432 Postgres admin connection. The gate project
(``127.0.0.1:15432``) is the one this repo already brings up; ``gate.sh``'s
``defence-suite``/``security-suite``/``db-tests`` steps export it automatically, so a gate
run needs nothing extra. Running the admin ``/admin/lab/run`` route or the CLI against a
plain local dev session instead needs the SAME variable exported on the backend process
first (start the gate project's database and export ``TEST_DATABASE_URL`` before asking for
a lab run) -- unset, every catalogue probe fails closed with a clear message, never a silent
skip counted as a pass (``SECRAG_REQUIRE_DB_TESTS=1`` is forced on the subprocess for the
same reason: a skip here would otherwise be indistinguishable from "already proven").

Results are written to ``defence_runs`` (T13.3.4) through the CALLING session -- the CLI's
own ``DATABASE_URL``, or the admin route's live request session -- a persistent table in
whatever long-lived database that is, independent of the harness's own disposable per-probe
database (each catalogue-probe test creates and drops its OWN ``secrag_test_*`` database,
same as every other ``db``-marked test; nothing here ever touches a shared account).

The erasure-at-scale probe (DEBT-005) is deliberately NOT part of this module's
catalogue-driven set: it needs its own throwaway Postgres SUPERUSER connection
(``SCALE_ADMIN_URL``), which only the gate project hands out -- see
``rag_app.devtools.erasure_scale`` (unchanged entry point; ``gate.sh``'s own
``erasure-scale`` step calls it exactly as before, now also asserting the "both runs in
``purger_runs``" DEBT-005 criterion). The negative control (DEBT-017 part 2, DA-G1-7) is a
ONE-OFF, throwaway-git-worktree proof (``rag_app.devtools.negative_control``), never run by
any gate step on purpose -- it is SUPPOSED to fail.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from rag_app.db.models import DefenceRun
from rag_app.devtools.threat_catalogue import extract_rows

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOGUE_FILE = REPO_ROOT / "docs" / "security" / "THREAT_MODEL.md"
PYTEST_TIMEOUT_SECONDS = 300
_EVIDENCE_MAX_CHARS = 2000

# T13.3.3: Phase 11-12 controls -- auth/sessions, CSRF, headers/CSP, rate limits,
# lockout/enumeration, ORM-level injection safety, cost limits (tokens counted locally).
# Every id here must be a `mitigated` row in the catalogue (checked at probe-build time, so
# a status that regresses to `planned` fails loudly instead of silently vanishing from the
# lab's probe list). REC-11/REC-12 (CSRF, server-side sessions) were added to the catalogue
# this block -- docs/security/THREAT_MODEL.md section 5.5 -- the only two of these categories
# that had no catalogue id at all before (CSRF/sessions lived only in the section 4 STRIDE
# prose, outside the parsed table).
#
# T13.J4-fix (DA-13-J3J4-2): each value is a short, static "what this attacks" one-liner --
# the SAME text that used to live only as an inline comment next to each id above, now a
# real value the admin defence-lab card can render (``Probe.description`` below) instead of
# the raw catalogue id alone. ``CATALOGUE_PROBE_IDS`` is this dict's own key order -- one
# list, never a second one that could silently drift out of sync.
CATALOGUE_PROBE_DESCRIPTIONS: dict[str, str] = {
    "OWASP-A02": "security misconfiguration (headers/CSP, backend half)",
    "OWASP-A05": "injection (ORM/parameterised queries)",
    "OWASP-A07": "authentication failures (timing, verified-email)",
    "API-1": "broken object level authorization (404, not a 403, enumeration-safe)",
    "API-2": "broken authentication",
    "API-4": "unrestricted resource consumption (rate limiting, daily cap)",
    "API-5": "broken function level authorization (permission map)",
    "API-6": "unrestricted access to sensitive business flows (lockout back-off)",
    "API-8": "security misconfiguration (docs/CORS)",
    "API-10": "unsafe consumption of upstream APIs",
    "LLM-10": "unbounded consumption (max_tokens -- cost limits, counted locally)",
    "REC-02": "daily answer cap on by default",
    "REC-04": "recovery-code single-use (no TOCTOU)",
    "REC-05": "login lockout counter (no TOCTOU)",
    "REC-06": "verification/reset token consume (no TOCTOU)",
    "REC-08": "route walker (auth + permission + rate limit on every route)",
    "REC-09": "exposed-files check",
    "REC-11": "CSRF double-submit token + Origin check",
    "REC-12": "server-side sessions (revocation)",
}

CATALOGUE_PROBE_IDS: tuple[str, ...] = tuple(CATALOGUE_PROBE_DESCRIPTIONS)


@dataclass(frozen=True)
class Probe:
    """One THREAT_MODEL-catalogue-keyed defence probe (T13.3.2)."""

    id: str
    expected_outcome: str
    test_id: str  # "path::test_function", resolved from the catalogue row
    # T13.J4-fix (DA-13-J3J4-2): display-only fields the admin defence-lab card renders
    # instead of the raw id (``Main.dc.html``'s own ``p.name``/``p.what``) -- never used by
    # ``run_catalogue_probes``/the pytest subprocess, which still key everything by ``id``/
    # ``test_id`` alone. Default "" so a Probe built outside ``catalogue_probes`` (e.g. a
    # test's own fake) never has to supply them.
    name: str = ""
    description: str = ""


@dataclass(frozen=True)
class ProbeOutcome:
    observed: str
    evidence: str
    passed: bool


@dataclass(frozen=True)
class ProbeResult:
    probe: Probe
    outcome: ProbeOutcome


class CatalogueProbeError(RuntimeError):
    """A requested probe id is not a ``mitigated`` THREAT_MODEL catalogue entry."""


def catalogue_probes(
    ids: Sequence[str] = CATALOGUE_PROBE_IDS, catalogue: Path = CATALOGUE_FILE
) -> list[Probe]:
    """Resolves every id to its catalogue row.

    Raises :class:`CatalogueProbeError` when an id is missing from the catalogue or is not
    ``mitigated`` -- a catalogue entry that regresses to ``planned``/``accepted`` must fail
    loudly here, not silently disappear from the lab's probe list.
    """
    rows = {
        row.entry_id: row for row in extract_rows(catalogue.read_text(encoding="utf-8"), catalogue)
    }
    probes = []
    for probe_id in ids:
        row = rows.get(probe_id)
        if row is None:
            raise CatalogueProbeError(f"'{probe_id}' is not a THREAT_MODEL catalogue entry")
        if row.status != "mitigated":
            raise CatalogueProbeError(
                f"'{probe_id}' is '{row.status}', not 'mitigated' -- remove it from"
                " CATALOGUE_PROBE_IDS or re-check its control"
            )
        probes.append(
            Probe(
                id=probe_id,
                expected_outcome=f"mitigated: {row.test_id} passes",
                test_id=row.test_id,
                name=row.entry or probe_id,
                description=CATALOGUE_PROBE_DESCRIPTIONS.get(probe_id, ""),
            )
        )
    return probes


def probe_display_info(
    ids: Sequence[str], catalogue: Path = CATALOGUE_FILE
) -> dict[str, tuple[str, str]]:
    """Maps every id in ``ids`` to ``(name, description)`` for display (DA-13-J3J4-2) --
    used for HISTORY rows (``GET /admin/lab/runs``), which only ever store a bare
    ``probe_id`` in ``defence_runs``. Unlike :func:`catalogue_probes`, this NEVER raises for
    a missing/non-mitigated id: a history row can legitimately name an id whose catalogue
    status changed (or which was removed from ``CATALOGUE_PROBE_IDS``) since it last ran --
    it still deserves a readable card, falling back to the bare id as its own name and ""
    as its description."""
    rows = {
        row.entry_id: row for row in extract_rows(catalogue.read_text(encoding="utf-8"), catalogue)
    }
    info: dict[str, tuple[str, str]] = {}
    for probe_id in ids:
        row = rows.get(probe_id)
        name = row.entry if row is not None and row.entry else probe_id
        info[probe_id] = (name, CATALOGUE_PROBE_DESCRIPTIONS.get(probe_id, ""))
    return info


def git_sha(root: Path = REPO_ROOT) -> str:
    """The current commit, or ``"unknown"`` -- never raises (a dirty/detached dev checkout
    must still be able to run the lab)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - evidence text only, never raise for this
        return "unknown"


_RESULT_LINE = re.compile(
    r"^(?P<nodeid>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR)\b", re.MULTILINE
)


def _backend_relative(test_id: str) -> str:
    """Catalogue test ids are repo-root-relative (``backend/tests/...``, the same convention
    ``threat_catalogue._test_exists`` checks); the subprocess below runs with ``cwd=backend``
    (same convention as ``gate.sh``'s own ``security-suite``/``db-tests`` steps), so the
    leading ``backend/`` must be stripped before it is passed as a pytest node id."""
    prefix = "backend/"
    return test_id[len(prefix) :] if test_id.startswith(prefix) else test_id


def run_catalogue_probes(probes: Sequence[Probe], *, root: Path = REPO_ROOT) -> list[ProbeResult]:
    """Runs every probe's test in ONE pytest subprocess (``-v``, one result line per test),
    then maps each probe back to its own line -- "one implementation" (A2): never a re-typed
    copy of the test's own assertions, the test itself IS the probe."""
    if not probes:
        return []
    if not os.environ.get("TEST_DATABASE_URL"):
        evidence = (
            "TEST_DATABASE_URL is not set -- the defence lab's catalogue probes need the"
            " same local test-DB harness as security-suite/db-tests (a local, non-5432"
            " Postgres admin connection -- the gate project, 127.0.0.1:15432, is the one this"
            " repo already brings up: backend/tests/db_harness.py). Start it, export"
            " TEST_DATABASE_URL on this process, then retry."
        )
        return [ProbeResult(probe, ProbeOutcome("not run", evidence, False)) for probe in probes]

    node_ids = [_backend_relative(p.test_id) for p in probes]
    env = {**os.environ, "SECRAG_REQUIRE_DB_TESTS": "1"}
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *node_ids,
                "-v",
                # The project's own pyproject.toml sets `addopts = "-q"` -- without cancelling
                # it here, `-q` wins over `-v` and pytest never prints the per-test
                # "nodeid PASSED/FAILED" lines this parser depends on.
                "-o",
                "addopts=",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=root / "backend",
            env=env,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        evidence = f"pytest timed out after {PYTEST_TIMEOUT_SECONDS}s ({exc})"
        return [ProbeResult(probe, ProbeOutcome("timeout", evidence, False)) for probe in probes]

    output = proc.stdout + "\n" + proc.stderr
    by_nodeid = [(m.group("nodeid"), m.group("status")) for m in _RESULT_LINE.finditer(output)]
    results = []
    for probe in probes:
        nodeid = _backend_relative(probe.test_id)
        # A parametrized test (e.g. REC-09's `test_the_backend_never_serves_any_of_these_
        # paths`) selected by its BARE node id runs every variant, each printed as
        # "nodeid[param] PASSED/FAILED" -- never the bare nodeid alone. Matches every
        # variant (exact id, or id immediately followed by "["); the control only counts as
        # matching its expected outcome when EVERY variant does.
        matches = [
            (line_id, status)
            for line_id, status in by_nodeid
            if line_id == nodeid or line_id.startswith(nodeid + "[")
        ]
        if not matches:
            tail = output[-_EVIDENCE_MAX_CHARS:]
            evidence = f"no result line for {nodeid} (pytest exit {proc.returncode}): {tail}"
            results.append(ProbeResult(probe, ProbeOutcome("not run", evidence, False)))
            continue
        passed = all(status == "PASSED" for _line_id, status in matches)
        observed = "passed" if passed else "failed"
        evidence = "; ".join(f"{line_id}: {status}" for line_id, status in matches)
        results.append(
            ProbeResult(probe, ProbeOutcome(observed, evidence[:_EVIDENCE_MAX_CHARS], passed))
        )
    return results


def record_results(
    session: Session, results: Sequence[ProbeResult], *, sha: str | None = None
) -> None:
    """Persists every result into ``defence_runs`` (T13.3.4). Caller commits -- same
    convention as ``rag_app.erasure._enqueue``/``rag_app.admin_audit.record_admin_action``."""
    resolved_sha = sha if sha is not None else git_sha()
    for result in results:
        session.add(
            DefenceRun(
                probe_id=result.probe.id,
                git_sha=resolved_sha,
                expected_outcome=result.probe.expected_outcome,
                observed_outcome=result.outcome.observed,
                passed=result.outcome.passed,
                evidence=result.outcome.evidence[:_EVIDENCE_MAX_CHARS],
            )
        )
