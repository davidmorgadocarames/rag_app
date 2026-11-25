"""T16.1.8: the pre-push hook requires a RECORDED ``scripts/gate.sh --k8s`` PASS only when
the pushed diff touches ``deploy/k8s/**``, a k8s probe implementation or a scaling path --
never for an unrelated branch/commit (the exact lesson from the gitleaks cross-branch
scoping issue: scope by the pushed DIFF, not by the branch name or by "this project ships
k8s manifests at all").

Builds a throwaway git repository (own ``.githooks/pre-push`` copy + a trivial ``scripts/
gate.sh`` stub that always exits 0, same pattern as ``test_gate_script.py``'s ``_make_repo``)
so these tests never touch the real clone's hook, gate or ``.gate/`` state, and never run the
real (slow) gate. Fails on the pre-T16.1.8 hook, which has no concept of ``deploy/k8s`` scope
at all and would happily push an unreviewed k8s manifest change with no ``--k8s`` evidence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SRC = REPO_ROOT / ".githooks" / "pre-push"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not HOOK_SRC.exists(), reason="needs bash and the real hook"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    (repo / "scripts" / "gate.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    shutil.copy(HOOK_SRC, repo / ".githooks" / "pre-push")
    for path in (repo / "scripts" / "gate.sh", repo / ".githooks" / "pre-push"):
        path.chmod(0o755)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_hook(repo: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / ".githooks" / "pre-push"), "origin", "git@github.com:x/y.git"],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _commit_file(repo: Path, relpath: str, content: str) -> str:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", f"add {relpath}")
    return _git(repo, "rev-parse", "HEAD")


def test_unrelated_commit_never_requires_a_k8s_pass(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit_file(repo, "README.md", "hello\n")
    proc = _run_hook(repo, f"refs/heads/feature {new} refs/heads/feature {base}\n")
    assert proc.returncode == 0, proc.stderr
    assert "k8s scope" not in proc.stdout


def test_k8s_touching_commit_blocked_without_a_recorded_pass(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit_file(repo, "deploy/k8s/base/foo.yaml", "kind: ConfigMap\n")
    proc = _run_hook(repo, f"refs/heads/feature {new} refs/heads/feature {base}\n")
    assert proc.returncode == 1
    assert "no matching .gate/k8s-pass PASS" in proc.stderr
    assert "gate.sh --k8s" in proc.stderr


def test_k8s_touching_commit_passes_with_a_matching_recorded_pass(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit_file(repo, "deploy/k8s/base/foo.yaml", "kind: ConfigMap\n")
    gate_dir = repo / ".gate"
    gate_dir.mkdir()
    (gate_dir / "k8s-pass").write_text(f"sha={new}\nat=2026-11-25T00:00:00Z\n", encoding="utf-8")
    proc = _run_hook(repo, f"refs/heads/feature {new} refs/heads/feature {base}\n")
    assert proc.returncode == 0, proc.stderr
    assert "recorded --k8s PASS found" in proc.stdout


def test_k8s_pass_recorded_for_a_different_sha_still_blocks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit_file(repo, "deploy/k8s/base/foo.yaml", "kind: ConfigMap\n")
    gate_dir = repo / ".gate"
    gate_dir.mkdir()
    (gate_dir / "k8s-pass").write_text(
        "sha=" + "0" * 40 + "\nat=2026-11-25T00:00:00Z\n", encoding="utf-8"
    )
    proc = _run_hook(repo, f"refs/heads/feature {new} refs/heads/feature {base}\n")
    assert proc.returncode == 1
    assert "no matching .gate/k8s-pass PASS" in proc.stderr


def test_probe_implementation_change_also_requires_a_k8s_pass(tmp_path: Path) -> None:
    """Scope is not only deploy/k8s/** -- the probe's own backend route code counts too."""
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit_file(repo, "backend/src/rag_app/api/app.py", "# probes live here\n")
    proc = _run_hook(repo, f"refs/heads/feature {new} refs/heads/feature {base}\n")
    assert proc.returncode == 1
    assert "no matching .gate/k8s-pass PASS" in proc.stderr


def test_dry_run_reports_the_plan_without_requiring_a_pass(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit_file(repo, "deploy/k8s/base/foo.yaml", "kind: ConfigMap\n")
    proc = subprocess.run(
        ["bash", str(repo / ".githooks" / "pre-push"), "origin", "git@github.com:x/y.git"],
        cwd=repo,
        input=f"refs/heads/feature {new} refs/heads/feature {base}\n",
        env={**_dry_run_env(), "PATH": _path_env()},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 10
    assert "a recorded --k8s PASS will be required" in proc.stdout


def _dry_run_env() -> dict[str, str]:
    import os

    return {**os.environ, "SECRAG_PREPUSH_DRY_RUN": "1"}


def _path_env() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")
