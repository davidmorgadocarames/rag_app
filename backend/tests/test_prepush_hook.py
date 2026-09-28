"""The pre-push hook picks the gate mode from the pushed refs (T11.0.5, F-2026-09-27-3)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".githooks" / "pre-push"
ZERO = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs bash and a git checkout",
)


def _hook(stdin: str, dry_run: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SECRAG_PREPUSH_DRY_RUN": "1" if dry_run else "0"}
    return subprocess.run(
        ["bash", str(HOOK), "origin", "git@github.com:x/y.git"],
        cwd=REPO_ROOT,
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_only_deletions_do_nothing_even_without_dry_run() -> None:
    proc = _hook(f"(delete) {ZERO} refs/heads/old-branch {SHA_A}\n", dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert "only deletions pushed" in proc.stdout
    assert "gate" in proc.stdout and "worktree" not in proc.stdout


def test_empty_stdin_does_nothing() -> None:
    proc = _hook("", dry_run=False)
    assert proc.returncode == 0
    assert "only deletions pushed" in proc.stdout


DRY_RUN_RC = 10  # never 0: a forgotten SECRAG_PREPUSH_DRY_RUN blocks the push (DA-B-11)


def test_phase_branch_push_runs_fast() -> None:
    proc = _hook(f"refs/heads/phase-11a {SHA_A} refs/heads/phase-11a {SHA_B}\n")
    assert proc.returncode == DRY_RUN_RC
    assert f"plan: gate --fast on {SHA_A}" in proc.stdout
    assert "push BLOCKED" in proc.stderr


def test_push_to_main_runs_full_and_skips_deletions() -> None:
    stdin = (
        f"(delete) {ZERO} refs/heads/tmp {SHA_B}\n"
        f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n"
        f"refs/heads/phase-11a {SHA_A} refs/heads/phase-11a {SHA_B}\n"
    )
    proc = _hook(stdin)
    assert proc.returncode == DRY_RUN_RC
    assert f"plan: gate --full on {SHA_A}\n" in proc.stdout  # one SHA, checked once
    assert "refs/heads/tmp: deletion" in proc.stdout
