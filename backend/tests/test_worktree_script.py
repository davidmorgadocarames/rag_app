"""scripts/dev/worktree.sh add|list|remove (T13.14.5, Paralelo-B).

Exercised against a throwaway git repo copied into tmp_path, the same isolation pattern
test_gate_script.py uses for scripts/gate.sh -- these tests never touch the real clone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKTREE_SH = REPO_ROOT / "scripts" / "dev" / "worktree.sh"

needs_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _make_main_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main"
    (repo / "scripts" / "dev").mkdir(parents=True)
    shutil.copy(WORKTREE_SH, repo / "scripts" / "dev" / "worktree.sh")
    (repo / "scripts" / "dev" / "worktree.sh").chmod(0o755)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _wt(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {
        k: v for k, v in os.environ.items() if not k.startswith("GATE_") and k != "SECRAG_WT_ROOT"
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "dev" / "worktree.sh"), *args],
        cwd=repo,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


@needs_tools
def test_add_creates_a_worktree_on_a_new_branch(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    proc = _wt(repo, "add", "demo-block", "demo-branch", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (wt_root / "demo-block" / "README.md").exists()
    assert "ready (branch demo-branch)" in proc.stdout
    assert not (wt_root / "demo-block" / "WORKTREE_NOTE.md").exists()  # printed, not written
    branches = _git(repo, "branch", "--list", "demo-branch").stdout
    assert "demo-branch" in branches


@needs_tools
def test_add_defaults_the_branch_name_to_the_block_id(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    proc = _wt(repo, "add", "solo-block", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    branches = _git(repo, "branch", "--list", "solo-block").stdout
    assert "solo-block" in branches


@needs_tools
def test_add_refuses_when_the_worktree_already_exists(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    assert _wt(repo, "add", "dup-block", SECRAG_WT_ROOT=str(wt_root)).returncode == 0
    proc = _wt(repo, "add", "dup-block", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 1
    assert "already exists" in proc.stdout + proc.stderr


@needs_tools
def test_list_shows_only_worktrees_under_the_configured_root(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    assert _wt(repo, "add", "list-block", SECRAG_WT_ROOT=str(wt_root)).returncode == 0
    proc = _wt(repo, "list", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "list-block" in proc.stdout
    assert str(repo) not in proc.stdout  # the main tree itself is not "under the root"


@needs_tools
def test_remove_cleans_up_the_worktree_and_prunes_metadata(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    assert _wt(repo, "add", "rm-block", SECRAG_WT_ROOT=str(wt_root)).returncode == 0
    proc = _wt(repo, "remove", "rm-block", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (wt_root / "rm-block").exists()
    assert "rm-block" not in _git(repo, "worktree", "list").stdout


@needs_tools
def test_remove_refuses_with_uncommitted_changes_unless_forced(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    assert _wt(repo, "add", "dirty-block", SECRAG_WT_ROOT=str(wt_root)).returncode == 0
    (wt_root / "dirty-block" / "README.md").write_text("changed\n", encoding="utf-8")

    proc = _wt(repo, "remove", "dirty-block", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 1
    assert "uncommitted changes" in proc.stdout + proc.stderr
    assert (wt_root / "dirty-block").exists()

    proc = _wt(repo, "remove", "dirty-block", "--force", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (wt_root / "dirty-block").exists()


@needs_tools
def test_remove_refuses_on_the_main_working_tree_and_leaves_it_intact(tmp_path: Path) -> None:
    # SECRAG_WT_ROOT pointing at the main tree's own PARENT, with a block name equal to
    # the main tree's basename, makes $WT_ROOT/$block resolve to the main tree itself --
    # the exact misconfiguration DA-PB-1 described (e.g. SECRAG_WT_ROOT=$HOME/proyectos
    # + block=rag_app). Before the fix, `git worktree remove --force` on the main tree
    # fails (exit 128, "is a main working tree") and the old code fell back to a blind
    # `rm -rf`, deleting the main tree. This must now be refused before either happens.
    repo = _make_main_repo(tmp_path)
    proc = _wt(repo, "remove", repo.name, SECRAG_WT_ROOT=str(tmp_path))
    assert proc.returncode == 1
    assert "main working tree" in proc.stdout + proc.stderr
    assert repo.exists()
    assert (repo / "README.md").exists()


@needs_tools
def test_remove_refuses_on_an_ancestor_of_the_main_working_tree(tmp_path: Path) -> None:
    # SECRAG_WT_ROOT=tmp_path with block="." makes $WT_ROOT/$block resolve to tmp_path
    # itself, which is an ANCESTOR of the main repo (tmp_path/main) rather than the main
    # tree exactly -- the other half of the DA-PB-1 guard.
    repo = _make_main_repo(tmp_path)
    proc = _wt(repo, "remove", ".", SECRAG_WT_ROOT=str(tmp_path))
    assert proc.returncode == 1
    assert "ancestor of the main working tree" in proc.stdout + proc.stderr
    assert repo.exists()
    assert (repo / "README.md").exists()


@needs_tools
def test_remove_refuses_a_directory_git_does_not_know_is_a_worktree(tmp_path: Path) -> None:
    # A plain directory under SECRAG_WT_ROOT that was never created by `worktree.sh add`
    # (so it has no entry in `git worktree list --porcelain`) must never be deleted --
    # the old blind `|| rm -rf "$wt"` fallback would have removed it unconditionally.
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    stray = wt_root / "not-a-worktree"
    stray.mkdir(parents=True)
    (stray / "keepme.txt").write_text("not a worktree\n", encoding="utf-8")

    proc = _wt(repo, "remove", "not-a-worktree", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 1
    assert "not a worktree registered" in proc.stdout + proc.stderr
    assert (stray / "keepme.txt").exists()


@needs_tools
def test_remove_never_deletes_the_branch_or_its_commits(tmp_path: Path) -> None:
    # DA-PB-2: `remove` only deletes the checkout, never the branch ref/commits shared
    # in .git -- make an unpushed commit inside the worktree, remove it, and confirm the
    # branch and that commit are still reachable from the main repo afterward.
    repo = _make_main_repo(tmp_path)
    wt_root = tmp_path / "wt"
    assert _wt(repo, "add", "survive-block", SECRAG_WT_ROOT=str(wt_root)).returncode == 0
    wt_dir = wt_root / "survive-block"

    (wt_dir / "unpushed.txt").write_text("unpushed work\n", encoding="utf-8")
    _git(wt_dir, "add", "-A")
    _git(wt_dir, "commit", "-q", "-m", "unpushed work")
    commit_sha = _git(wt_dir, "rev-parse", "HEAD").stdout.strip()

    proc = _wt(repo, "remove", "survive-block", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not wt_dir.exists()

    branches = _git(repo, "branch", "--list", "survive-block").stdout
    assert "survive-block" in branches
    assert _git(repo, "cat-file", "-e", commit_sha).returncode == 0
    log = _git(repo, "log", "survive-block", "--oneline").stdout
    assert "unpushed work" in log


@needs_tools
def test_add_symlinks_env_files_from_the_main_tree(tmp_path: Path) -> None:
    repo = _make_main_repo(tmp_path)
    # "backend/" must be a TRACKED directory (as it is in the real repo) so the new
    # worktree has it at all -- a new worktree only populates tracked paths.
    (repo / "backend").mkdir()
    (repo / "backend" / "dummy.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add backend/")
    (repo / ".env").write_text("X=1\n", encoding="utf-8")  # never committed/tracked
    (repo / "backend" / ".env").write_text("Y=2\n", encoding="utf-8")  # same
    wt_root = tmp_path / "wt"

    proc = _wt(repo, "add", "env-block", SECRAG_WT_ROOT=str(wt_root))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    root_link = wt_root / "env-block" / ".env"
    backend_link = wt_root / "env-block" / "backend" / ".env"
    assert root_link.is_symlink()
    assert root_link.resolve() == (repo / ".env").resolve()
    assert backend_link.is_symlink()
    assert backend_link.resolve() == (repo / "backend" / ".env").resolve()
