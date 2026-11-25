"""T16.2.3 self-reported gap, found by this block's own node-drain chaos drill:
``scripts/k8s/deploy-local.sh``'s ``stage_foundation()`` applies every resource kind with no
ordering dependency on CNPG/roles/migrate/apps (Services, ConfigMaps, SealedSecrets, ...) --
but its ``awk`` filter predated the ``PodDisruptionBudget``s this block added
(``deploy/k8s/base/backend/pdb.yaml``, ``deploy/k8s/base/frontend/pdb.yaml``), so a real cluster
deployed via this script never actually applied them. Confirmed live: `kubectl drain` evicted
the backend pod despite the committed manifest declaring a PDB that should have blocked it,
because the PDB object was never created on the cluster at all.

This test runs the REAL ``awk`` program extracted from the script (never a hand-copied
duplicate -- a hand-copy could pass while the shipped code silently diverges) against a
synthetic ``index.txt``, so it fails on the pre-fix code (confirmed by reverting the fix
locally and re-running: the ``PodDisruptionBudget`` line is NOT selected) and passes on the
fixed code. No cluster, no kustomize binary needed -- pure text processing, wired into the
``pytest`` gate step like ``test_k8s_deploy_order.py``."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "k8s" / "deploy-local.sh"

# Matches the `awk -F: '...'` program inside stage_foundation(), up to the closing quote right
# before the `"$render_dir/index.txt"` argument that follows it in the real script.
_FOUNDATION_AWK_RE = re.compile(
    r"stage_foundation\(\).*?awk -F: '(.*?)'\s*\"\$render_dir/index\.txt\"",
    re.S,
)


def _foundation_awk_program() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = _FOUNDATION_AWK_RE.search(text)
    assert match, (
        "could not find stage_foundation()'s `awk -F: '...'` program in "
        f"{SCRIPT} -- did its shape change?"
    )
    return match.group(1)


def _select(awk_program: str, index_lines: list[str]) -> list[str]:
    index_text = "\n".join(index_lines) + "\n"
    result = subprocess.run(
        ["awk", "-F:", awk_program],
        input=index_text,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_foundation_awk_program_is_found() -> None:
    """Guards against the regex above silently matching nothing and every test below passing
    vacuously (an empty awk program selects nothing, which would look like every test passed
    for the wrong reason)."""
    assert _foundation_awk_program().strip()


def test_foundation_stage_includes_poddisruptionbudgets() -> None:
    program = _foundation_awk_program()
    selected = _select(
        program,
        [
            "/tmp/doc-0000.yaml:PodDisruptionBudget:backend",
            "/tmp/doc-0001.yaml:PodDisruptionBudget:frontend",
            "/tmp/doc-0002.yaml:Deployment:backend",
            "/tmp/doc-0003.yaml:Service:backend",
        ],
    )
    assert "/tmp/doc-0000.yaml" in selected
    assert "/tmp/doc-0001.yaml" in selected


def test_foundation_stage_still_excludes_backend_and_frontend_deployments() -> None:
    """Regression guard: the PDB fix must not accidentally widen the filter to also pick up
    the backend/frontend Deployments themselves, which stay gated behind migrate (DA-16-B-1)."""
    program = _foundation_awk_program()
    selected = _select(
        program,
        [
            "/tmp/doc-0000.yaml:Deployment:backend",
            "/tmp/doc-0001.yaml:Deployment:frontend",
            "/tmp/doc-0002.yaml:Deployment:valkey",
            "/tmp/doc-0003.yaml:PodDisruptionBudget:backend",
        ],
    )
    assert selected == ["/tmp/doc-0002.yaml", "/tmp/doc-0003.yaml"]
