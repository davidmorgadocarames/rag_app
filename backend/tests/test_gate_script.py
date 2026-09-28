"""scripts/gate.sh behaviour that protects the gate itself (DA-B-1, DA-B-2, DA-B-4, DA-B-9).

The script is copied into a throwaway git repository, so these tests never touch the real
clone's configuration, hooks or venv.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from rag_app.devtools import ollama_digest, venv_sync

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "gate.sh"

needs_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path, hooks_path: str | None = ".githooks") -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    shutil.copy(GATE, repo / "scripts" / "gate.sh")
    hook = repo / ".githooks" / "pre-push"
    hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    tool = repo / "tool"  # a script without .sh, found by its shebang
    tool.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    for path in (repo / "scripts" / "gate.sh", hook, tool):
        path.chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    if hooks_path:
        _git(repo, "config", "core.hooksPath", hooks_path)
    return repo


def _gate(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    # Drop what an enclosing gate run exports (GATE_MAIN_ROOT, GATE_VENV, GATE_MODE, …) so
    # the copy checks only the throwaway repository.
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GATE_") and k not in {"GITHUB_ACTIONS", "GIT_DIR", "GIT_WORK_TREE"}
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "gate.sh"), *args],
        cwd=repo,
        env={**base, "GATE_VENV": str(repo / "no-venv"), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


@needs_tools
def test_git_modes_passes_with_active_hook_and_executable_scripts(tmp_path: Path) -> None:
    proc = _gate(_make_repo(tmp_path), "--only", "git-modes")
    assert proc.returncode == 0, proc.stdout
    assert "3 tracked scripts" in proc.stdout
    assert "pre-push hook active" in proc.stdout


@needs_tools
def test_git_modes_fails_when_the_hook_is_not_active(tmp_path: Path) -> None:
    proc = _gate(_make_repo(tmp_path, hooks_path=None), "--only", "git-modes")
    assert proc.returncode == 1
    assert "core.hooksPath is 'unset'" in proc.stdout


@needs_tools
def test_git_modes_fails_when_the_hook_lost_its_exec_bit_on_disk(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".githooks" / "pre-push").chmod(0o644)  # what a UNC write does
    proc = _gate(repo, "--only", "git-modes")
    assert proc.returncode == 1
    assert "not executable on disk" in proc.stdout
    assert "git ignores it silently" in proc.stdout


@needs_tools
def test_git_modes_fails_on_a_non_executable_script_in_the_index(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _git(repo, "update-index", "--chmod=-x", "tool")
    proc = _gate(repo, "--only", "git-modes")
    assert proc.returncode == 1
    assert "not 100755 in git" in proc.stdout and "tool" in proc.stdout


@needs_tools
def test_git_modes_in_ci_skips_only_the_hook_activation(tmp_path: Path) -> None:
    proc = _gate(
        _make_repo(tmp_path, hooks_path=None), "--only", "git-modes", GITHUB_ACTIONS="true"
    )
    assert proc.returncode == 0, proc.stdout
    assert "not applicable in CI" in proc.stdout


@needs_tools
def test_only_with_missing_tooling_fails_instead_of_skipping(tmp_path: Path) -> None:
    """DA-B-2: a step named in --only never passes without running."""
    proc = _gate(_make_repo(tmp_path), "--only", "gitleaks")
    assert proc.returncode == 1
    assert "FAIL (missing tooling under --only)" in proc.stdout
    assert "GATE: FAIL" in proc.stdout


def test_the_real_hook_and_gate_are_executable_in_git() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "--", ".githooks/pre-push", "scripts/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes = {line.split()[3]: line.split()[0] for line in out.splitlines()}
    assert modes[".githooks/pre-push"] == "100755"
    assert modes["scripts/gate.sh"] == "100755"
    assert os.stat(GATE).st_mode & stat.S_IXUSR


# --- venv_sync (DA-B-4) -----------------------------------------------------------------


def test_pins_follow_includes_and_normalize_names(tmp_path: Path) -> None:
    (tmp_path / "base.txt").write_text(
        "# c\nPyJWT==2.15.0\npsycopg[binary]==3.2.3  # x\n", encoding="utf-8"
    )
    (tmp_path / "dev.txt").write_text("-r base.txt\nruff==0.8.4\n", encoding="utf-8")
    assert venv_sync.read_pins(tmp_path / "dev.txt") == {
        "pyjwt": "2.15.0",
        "psycopg": "3.2.3",
        "ruff": "0.8.4",
    }


def test_drift_reports_wrong_and_missing_versions() -> None:
    pins = {"pyjwt": "2.15.0", "ruff": "0.8.4", "pytest": "9.1.1"}
    installed = {"pyjwt": "2.10.1", "pytest": "9.1.1"}
    assert venv_sync.drift(pins, installed) == [
        "pyjwt: pinned 2.15.0, installed 2.10.1",
        "ruff: pinned 0.8.4, not installed",
    ]


def test_the_test_venv_matches_the_repository_pins() -> None:
    assert venv_sync.main(["--root", str(REPO_ROOT)]) == 0


# --- ollama_digest (DA-B-9) -------------------------------------------------------------

TAGS = {
    "models": [
        {"name": "qwen2.5:7b-instruct-q4_K_M", "digest": "aaa"},
        {"name": "bge-m3:latest", "model": "bge-m3:latest", "digest": "bbb"},
    ]
}


def test_a_bare_model_name_means_latest() -> None:
    assert ollama_digest.model_digest(TAGS, "bge-m3") == "bbb"
    assert ollama_digest.model_digest(TAGS, "bge-m3:latest") == "bbb"


def test_a_model_not_served_has_no_digest() -> None:
    assert ollama_digest.model_digest(TAGS, "nomic-embed-text") is None
    assert ollama_digest.model_digest({}, "bge-m3") is None
