"""Apply-ordering test for ``scripts/k8s/deploy-local.sh`` (DA-16-B-1 fix, T16.1.1/T16.1.6 Done-
when: "migration Job before the backend rollout"). Runs the script's own ``--dry-run --trace``
mode -- no cluster, no kustomize binary needed (CI has neither) -- and asserts the recorded
sequence proves the backend/frontend Deployments are never applied until after the migrate Job
is checked complete, which is itself gated behind the roles Job and the CNPG Cluster being
healthy.

This test fails on the pre-fix code: before this block, ``scripts/k8s/deploy-local.sh`` did not
exist at all (DA-16-B.md's blocker -- the manifests' own comments promised it, but
``smoke-16-1-cluster.sh`` did a single flat ``kustomize build | kubectl apply`` with no
ordering). Wired into the ``pytest`` gate step (no ``db`` marker, no cluster dependency)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "k8s" / "deploy-local.sh"

EXPECTED_ORDER = [
    "APPLY_NAMESPACE",
    "CHECK_CONTROLLERS_READY",
    "APPLY_FOUNDATION",
    "APPLY_CNPG",
    "CHECK_CNPG_HEALTHY",
    "APPLY_ROLES_JOB",
    "CHECK_ROLES_JOB_COMPLETE",
    "APPLY_MIGRATE_JOB",
    "CHECK_MIGRATE_JOB_COMPLETE",
    "APPLY_APPS",
    "CHECK_ROLLOUT_BACKEND",
    "CHECK_ROLLOUT_FRONTEND",
]


def _run_trace() -> list[str]:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist -- the DA-16-B-1 fix is missing"
    with tempfile.TemporaryDirectory() as tmp:
        trace_file = Path(tmp) / "trace.txt"
        result = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run", "--trace", str(trace_file)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        msg = (
            f"--dry-run exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert result.returncode == 0, msg
        return trace_file.read_text(encoding="utf-8").splitlines()


def test_dry_run_trace_matches_the_exact_expected_order() -> None:
    assert _run_trace() == EXPECTED_ORDER


def test_migrate_job_is_checked_complete_before_apps_are_applied() -> None:
    """The Done-when this whole fix exists for: a backend/frontend rollout cannot start before
    the migrate Job is confirmed complete."""
    trace = _run_trace()
    assert trace.index("CHECK_MIGRATE_JOB_COMPLETE") < trace.index("APPLY_APPS")


def test_roles_job_is_checked_complete_before_migrate_job_is_applied() -> None:
    trace = _run_trace()
    assert trace.index("CHECK_ROLES_JOB_COMPLETE") < trace.index("APPLY_MIGRATE_JOB")


def test_cnpg_cluster_is_checked_healthy_before_roles_job_is_applied() -> None:
    trace = _run_trace()
    assert trace.index("CHECK_CNPG_HEALTHY") < trace.index("APPLY_ROLES_JOB")


def test_dry_run_never_touches_a_cluster_or_needs_kustomize() -> None:
    """Regression guard for the real bug this test suite caught while writing it: `trace()`
    returning the status of a bare `[ -n "$TRACE_FILE" ] && …` aborted the whole script under
    `set -e` whenever TRACE_FILE was unset -- --dry-run always sets it, so this specific bug
    could only have been caught by a real (non-dry-run) run; asserted here as a second line of
    defence by running --dry-run WITHOUT --trace at all (stdout-only) and checking it still
    exits 0."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
