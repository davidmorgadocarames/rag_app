"""scripts/gate.sh behaviour that protects the gate itself (DA-B-1, DA-B-2, DA-B-4, DA-B-9).

The script is copied into a throwaway git repository, so these tests never touch the real
clone's configuration, hooks or venv.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import time
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


def _gate_env(repo: Path, **env: str) -> dict[str, str]:
    # Drop what an enclosing gate run exports (GATE_MAIN_ROOT, GATE_VENV, GATE_MODE, …) so
    # the copy checks only the throwaway repository. XDG_RUNTIME_DIR is dropped too
    # (T13.14.4): gate.sh's global lock defaults to ${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag
    # -- if the real ambient session value leaked through, every test below would share
    # ONE real, per-login-session lock directory regardless of the scratch HOME used for
    # everything else, and could even queue behind a real gate.sh run on the dev machine.
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GATE_")
        and k not in {"GITHUB_ACTIONS", "GIT_DIR", "GIT_WORK_TREE", "XDG_RUNTIME_DIR"}
    }
    # DA-E-1: every call gets its own scratch HOME by default, so gate.sh's default
    # GATE_LOG_DIR=$HOME/secrag-gate-logs (T11c.E1) never writes a test-generated log into
    # the REAL ~/secrag-gate-logs -- which also holds the orchestrator's own promotion
    # evidence. A test that exercises the logging behaviour itself passes its own HOME (or
    # GATE_LOG_DIR) in **env, which wins (dict union below keeps the last value).
    scratch_home = {"HOME": str(repo.parent / "scratch-home")}
    return {**base, "GATE_VENV": str(repo / "no-venv"), **scratch_home, **env}


def _gate(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "gate.sh"), *args],
        cwd=repo,
        env=_gate_env(repo, **env),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _resolve_seed_files(repo: Path, head: str, **env: str) -> subprocess.CompletedProcess[str]:
    """F31: exercises resolve_seed_files() via gate.sh's own --resolve-seed-files test-only
    re-entrant entry point -- plain files on disk, no Docker/DB/lock/log needed."""
    return subprocess.run(
        ["bash", str(repo / "scripts" / "gate.sh"), "--resolve-seed-files", head],
        cwd=repo,
        env=_gate_env(repo, **env),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _shell(repo: Path, script: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Runs a raw shell snippet (not a fixed argv) so a test can wrap `scripts/gate.sh` in a
    manual `&`, `nohup … &` or `( … & )` the same way a human/agent would from a terminal --
    the thing F24 is about. Uses the same scratch HOME/env isolation as _gate()."""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        env=_gate_env(repo, **env),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _gate_popen(repo: Path, *args: str, **env: str) -> subprocess.Popen[str]:
    # new_session=True (setsid): the process and anything it forks (e.g. the test-only
    # GATE_LOCK_TEST_SLEEP `sleep`) share ONE process group, so a test can release the lock
    # deterministically with a single killpg instead of racing a lone `kill -9 <pid>`
    # against a still-running forked child that inherited a copy of the lock fd.
    return subprocess.Popen(
        ["bash", str(repo / "scripts" / "gate.sh"), *args],
        cwd=repo,
        env=_gate_env(repo, **env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
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


@needs_tools
def test_only_fails_in_the_main_tree_when_the_venv_drifted_from_the_pins(tmp_path: Path) -> None:
    """DA-B2-2: `--only <step>` without the venv step must not pass on a drifted venv."""
    repo = _make_repo(tmp_path)
    fake_python = repo / "backend" / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # venv_sync: drift
    fake_python.chmod(0o755)
    # No GATE_VENV: the gate resolves the main tree's venv (the fake one above). HOME is
    # still scratch (and XDG_RUNTIME_DIR dropped) so the lock/log dirs stay out of the
    # real ~/.cache and ~/secrag-gate-logs (DA-E-1 / T13.14.4), even though this test calls
    # subprocess.run directly instead of the _gate() helper.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GATE_")
        and k not in {"GITHUB_ACTIONS", "GIT_DIR", "GIT_WORK_TREE", "XDG_RUNTIME_DIR"}
    }
    env["HOME"] = str(tmp_path / "scratch-home")
    proc = subprocess.run(
        ["bash", str(repo / "scripts" / "gate.sh"), "--only", "git-modes"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stdout
    assert "does not match this tree's pins" in proc.stdout
    assert "GATE: FAIL" in proc.stdout


@needs_tools
def test_a_normal_gate_call_never_touches_the_real_home_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-E-1: every other test in this file calls _gate() without passing HOME/GATE_LOG_DIR
    -- they must never write into the real ~/secrag-gate-logs just because the enclosing
    pytest process's own HOME leaked through. Simulates that real HOME with a sentinel dir
    standing in for it and asserts _gate() never touches it."""
    pretend_real_home = tmp_path / "pretend-real-home"
    sentinel = pretend_real_home / "secrag-gate-logs"
    sentinel.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(pretend_real_home))
    proc = _gate(_make_repo(tmp_path), "--only", "git-modes")
    assert proc.returncode == 0, proc.stdout
    assert list(sentinel.iterdir()) == []  # untouched: no gate-*.log landed here


@needs_tools
def test_a_real_run_logs_outside_tmp_under_home_by_default(tmp_path: Path) -> None:
    """C8c / T11c.E1: evidence must survive a WSL stop (/tmp is tmpfs); it never lives there."""
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    proc = _gate(repo, "--only", "git-modes", HOME=str(home))
    assert proc.returncode == 0, proc.stdout
    assert "GATE: PASS" in proc.stdout  # still printed to the terminal, not only to the file
    log_dir = home / "secrag-gate-logs"
    logs = list(log_dir.glob("gate-*.log"))
    assert len(logs) == 1, logs
    # The default is derived from HOME, never a literal /tmp path (which is tmpfs under WSL).
    assert str(logs[0]).startswith(str(home))
    content = logs[0].read_text(encoding="utf-8")
    assert "GATE: PASS" in content
    assert f"gate log: {logs[0]}" in proc.stderr


@needs_tools
def test_the_log_dir_and_file_are_private(tmp_path: Path) -> None:
    """DA-E-2: the directory is 0700 and every log file 0600, even though $HOME (and
    anything under it) may default to the much looser 0755/0644 a plain mkdir/touch would
    use -- a local umask, not the caller's, decides this."""
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    proc = _gate(repo, "--only", "git-modes", HOME=str(home))
    assert proc.returncode == 0, proc.stdout
    log_dir = home / "secrag-gate-logs"
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    logs = list(log_dir.glob("gate-*.log"))
    assert len(logs) == 1, logs
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600


@needs_tools
def test_a_pre_existing_looser_log_dir_is_tightened(tmp_path: Path) -> None:
    """DA-E-2: a directory from before this check existed (or created some other way) is
    tightened on the very next run, not left at whatever it had."""
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    log_dir = home / "secrag-gate-logs"
    log_dir.mkdir(parents=True)
    log_dir.chmod(0o755)
    proc = _gate(repo, "--only", "git-modes", HOME=str(home))
    assert proc.returncode == 0, proc.stdout
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700


@needs_tools
def test_gate_log_dir_is_configurable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    custom = tmp_path / "custom-gate-logs"
    # T13.14.4: the global lock also defaults under $HOME (GATE_LOCK_DIR) -- overridden here
    # too, like GATE_LOG_DIR, so this test still proves HOME itself is never touched when
    # every HOME-derived default is overridden.
    proc = _gate(
        repo,
        "--only",
        "git-modes",
        HOME=str(home),
        GATE_LOG_DIR=str(custom),
        GATE_LOCK_FILE=str(tmp_path / "gate.lock"),
    )
    assert proc.returncode == 0, proc.stdout
    assert not home.exists()  # the default directory is never created when overridden
    assert list(custom.glob("gate-*.log"))


@needs_tools
def test_informational_flags_never_open_a_log_file(tmp_path: Path) -> None:
    """--list is not a gate run: it must not leave a log file (or create the directory)."""
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    proc = _gate(repo, "--list", HOME=str(home))
    assert proc.returncode == 0, proc.stdout
    assert not (home / "secrag-gate-logs").exists()


@needs_tools
def test_list_includes_the_12a_e_steps_as_stack_only_not_fast(tmp_path: Path) -> None:
    """T12.2.5/T12.2.6 (E9): `security-headers`/`zap-baseline` need the running
    frontend+backend (the gate stack), so they must never show up in `--fast`'s step set --
    only `--only`/`--full`. `--list`'s own output is the single source of truth for "which
    steps exist and what they need" (no separate step-name list to drift)."""
    proc = _gate(_make_repo(tmp_path), "--list")
    assert proc.returncode == 0, proc.stdout
    lines = {
        line.split()[0]: line.split()[1] for line in proc.stdout.splitlines() if line.split()[:1]
    }
    for name in ("security-headers", "zap-baseline"):
        assert name in lines, f"{name} missing from --list"
        assert (
            lines[name] != "-"
        ), f"{name} must need the gate stack (not '-', or it would run in --fast)"


@needs_tools
def test_only_security_headers_and_zap_baseline_are_unknown_in_a_repo_without_the_app(
    tmp_path: Path,
) -> None:
    """A cheap, no-stack-needed sanity check that both new steps are real, dispatchable step
    names (is_step), the same way every other step name is validated by --only."""
    proc = _gate(_make_repo(tmp_path), "--only", "security-headers,zap-baseline,not-a-real-step")
    assert proc.returncode == 2, proc.stdout
    assert "unknown step: not-a-real-step" in proc.stdout + proc.stderr


# --- F24 guard: refuse a manual-background launch (DA-13-HI-2, 2026-11-25) -------------
#
# A shell backgrounds a command non-interactively (`cmd &`, `nohup cmd &`, `( cmd & )`) by
# setting BOTH SIGINT and SIGQUIT to ignored for it -- and that ignored disposition is
# preserved across every exec() the command (or anything it forks) does, which is what
# broke test_db_tunnel.py's signal tests three times on 2026-11-25. These tests prove
# gate.sh refuses to start in exactly those three launch styles, and keeps working for
# every sanctioned launch (plain foreground, the --run-step re-entrant child, and the
# documented escape hatch).


@needs_tools
def test_refuses_a_manual_background_launch_with_bare_amp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _shell(repo, "bash scripts/gate.sh --only git-modes & wait $!")
    assert proc.returncode == 1
    out = proc.stdout + proc.stderr
    assert "F24" in out
    assert "GATE_BACKGROUND_OK=1" in out  # the documented escape hatch is named


@needs_tools
def test_refuses_a_nohup_background_launch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _shell(repo, "nohup bash scripts/gate.sh --only git-modes & wait $!")
    assert proc.returncode == 1
    assert "F24" in proc.stdout + proc.stderr


@needs_tools
def test_refuses_a_parenthesised_background_subshell(tmp_path: Path) -> None:
    """`( cmd & )` -- the subshell itself exits immediately (so its own return code says
    nothing), but the backgrounded gate.sh inside it must still refuse to run."""
    repo = _make_repo(tmp_path)
    out_file = tmp_path / "paren.out"
    _shell(
        repo,
        f"( bash scripts/gate.sh --only git-modes > '{out_file}' 2>&1 & ); sleep 1",
    )
    assert "F24" in out_file.read_text(encoding="utf-8")


@needs_tools
def test_a_plain_foreground_launch_is_never_refused(tmp_path: Path) -> None:
    proc = _gate(_make_repo(tmp_path), "--only", "git-modes")
    assert proc.returncode == 0, proc.stdout
    assert "F24" not in proc.stdout + proc.stderr


@needs_tools
def test_the_run_step_reentrant_child_is_never_refused(tmp_path: Path) -> None:
    """The pre-push hook, CI (`gate.sh --only …`) and every step's own `bash "$SELF"
    --run-step <name>` child all run gate.sh as a plain foreground child of an
    already-running (non-backgrounded) process -- never through a shell `&`. Exercised
    directly via the re-entrant --run-step path."""
    proc = _gate(_make_repo(tmp_path), "--run-step", "environment")
    assert "F24" not in proc.stdout + proc.stderr


@needs_tools
def test_the_escape_hatch_env_var_allows_a_deliberate_background_launch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _shell(
        repo,
        "bash scripts/gate.sh --only git-modes & wait $!",
        GATE_BACKGROUND_OK="1",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "F24" not in proc.stdout + proc.stderr


# --- Ollama reproducibility warning (DEC-066 change 6, F24-into-14) --------------------
#
# The dialogue eval (DEC-064) is only byte-reproducible when the native `ollama serve` runs
# with OLLAMA_NUM_PARALLEL=1. gate.sh now warns (never fails) when it detects the native
# server lacks it. GATE_OLLAMA_PID_FOR_TEST is a test-only override (see gate.sh) so these
# tests never race the real pgrep against whatever "ollama serve" may already be running on
# the host machine.


@needs_tools
def test_warns_when_the_native_server_lacks_num_parallel_one(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env_without = {k: v for k, v in os.environ.items() if k != "OLLAMA_NUM_PARALLEL"}
    fake = subprocess.Popen(["sleep", "30"], env=env_without)
    try:
        proc = _gate(repo, "--only", "git-modes", GATE_OLLAMA_PID_FOR_TEST=str(fake.pid))
    finally:
        fake.terminate()
        fake.wait(timeout=10)
    out = proc.stdout + proc.stderr
    assert "WARNING" in out
    assert "OLLAMA_NUM_PARALLEL=1" in out
    assert str(fake.pid) in out


@needs_tools
def test_warns_when_the_native_server_has_a_different_value(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # A real, separate throwaway process with its OWN environ (os.getpid() above is the
    # pytest process itself, whose environ is fixed at its own startup -- a subprocess.run
    # env= override only affects a NEW child, never retroactively changes another already-
    # running process's /proc/<pid>/environ).
    fake = subprocess.Popen(["sleep", "30"], env={**os.environ, "OLLAMA_NUM_PARALLEL": "4"})
    try:
        proc = _gate(repo, "--only", "git-modes", GATE_OLLAMA_PID_FOR_TEST=str(fake.pid))
    finally:
        fake.terminate()
        fake.wait(timeout=10)
    out = proc.stdout + proc.stderr
    assert "WARNING" in out
    assert "'4'" in out


@needs_tools
def test_no_warning_when_the_native_server_has_num_parallel_one(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    fake = subprocess.Popen(["sleep", "30"], env={**os.environ, "OLLAMA_NUM_PARALLEL": "1"})
    try:
        proc = _gate(repo, "--only", "git-modes", GATE_OLLAMA_PID_FOR_TEST=str(fake.pid))
    finally:
        fake.terminate()
        fake.wait(timeout=10)
    out = proc.stdout + proc.stderr
    assert "WARNING" not in out


@needs_tools
def test_no_warning_and_no_crash_when_no_server_is_found(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # A PID that (almost certainly) does not exist -- simulates "no native server running"
    # deterministically, without depending on whether a real ollama happens to be up.
    proc = _gate(repo, "--only", "git-modes", GATE_OLLAMA_PID_FOR_TEST="999999999")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "WARNING" not in out


# --- F31: one gate seed per alembic head (2026-11-25) -----------------------------------
#
# $GATE_STATE (.gate/) is shared across every worktree of this repo (it lives under
# MAIN_ROOT, not per-branch) -- a single top-level seed.dump/seed.meta broke eval/latency/e2e
# on every branch whose alembic head differed from whichever branch last ran --make-seed
# (14-C3 rebuilt it at 0009_conversation_summary; every 0008 branch, including main, failed).
# These tests exercise resolve_seed_files() directly (no Docker/DB needed) via gate.sh's own
# --resolve-seed-files test-only entry point.


@needs_tools
def test_no_seed_anywhere_names_make_seed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _resolve_seed_files(repo, "0008_admin_dashboard")
    assert proc.returncode == 1
    assert "SEED_ERROR=no gate seed for alembic head 0008_admin_dashboard" in proc.stdout
    assert "scripts/gate.sh --make-seed" in proc.stdout


@needs_tools
def test_the_per_head_directory_is_used_when_present(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    seed_dir = repo / ".gate" / "seeds" / "0009_conversation_summary"
    seed_dir.mkdir(parents=True)
    (seed_dir / "seed.dump").write_bytes(b"dump")
    (seed_dir / "seed.meta").write_text("alembic_head=0009_conversation_summary\n")
    proc = _resolve_seed_files(repo, "0009_conversation_summary")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SEED_FILE={seed_dir}/seed.dump" in proc.stdout
    assert f"SEED_META_FILE={seed_dir}/seed.meta" in proc.stdout


@needs_tools
def test_a_different_heads_directory_is_never_picked(tmp_path: Path) -> None:
    """Two branches on different heads, each with its own per-head directory, must never
    see each other's seed."""
    repo = _make_repo(tmp_path)
    for head in ("0008_admin_dashboard", "0009_conversation_summary"):
        d = repo / ".gate" / "seeds" / head
        d.mkdir(parents=True)
        (d / "seed.dump").write_bytes(f"dump-{head}".encode())
        (d / "seed.meta").write_text(f"alembic_head={head}\n")
    proc = _resolve_seed_files(repo, "0008_admin_dashboard")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "seeds/0008_admin_dashboard/seed.dump" in proc.stdout
    assert "0009_conversation_summary" not in proc.stdout


@needs_tools
def test_the_legacy_top_level_seed_is_used_when_it_matches_the_head(tmp_path: Path) -> None:
    """Backward compatibility (F31): a tree that predates the per-head layout still works
    as long as the legacy seed's own recorded head matches what is being asked for."""
    repo = _make_repo(tmp_path)
    gate_dir = repo / ".gate"
    gate_dir.mkdir()
    (gate_dir / "seed.dump").write_bytes(b"legacy-dump")
    (gate_dir / "seed.meta").write_text("alembic_head=0008_admin_dashboard\n")
    proc = _resolve_seed_files(repo, "0008_admin_dashboard")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SEED_FILE={gate_dir}/seed.dump" in proc.stdout
    assert f"SEED_META_FILE={gate_dir}/seed.meta" in proc.stdout


@needs_tools
def test_the_legacy_seed_is_rejected_with_a_clear_message_when_its_head_differs(
    tmp_path: Path,
) -> None:
    """The exact scenario this block fixes: some other branch/worktree rebuilt the shared
    legacy seed at a different head (14-C3 at 0009 while this tree is at 0008) -- the error
    must name both the requested head and the legacy seed's actual head, not silently use
    the wrong data."""
    repo = _make_repo(tmp_path)
    gate_dir = repo / ".gate"
    gate_dir.mkdir()
    (gate_dir / "seed.dump").write_bytes(b"legacy-dump")
    (gate_dir / "seed.meta").write_text("alembic_head=0009_conversation_summary\n")
    proc = _resolve_seed_files(repo, "0008_admin_dashboard")
    assert proc.returncode == 1
    assert "SEED_ERROR=no gate seed for alembic head 0008_admin_dashboard" in proc.stdout
    assert "the legacy seed at" in proc.stdout
    assert "is for head 0009_conversation_summary" in proc.stdout
    assert "scripts/gate.sh --make-seed" in proc.stdout


@needs_tools
def test_the_per_head_directory_wins_over_a_stale_legacy_seed_for_the_same_head(
    tmp_path: Path,
) -> None:
    """If --make-seed has already written the modern per-head directory for this exact
    head, it is used even though an old legacy top-level seed for the same head also still
    exists on disk (--make-seed never deletes the legacy files, it only stops writing them)."""
    repo = _make_repo(tmp_path)
    gate_dir = repo / ".gate"
    gate_dir.mkdir()
    (gate_dir / "seed.dump").write_bytes(b"legacy-dump")
    (gate_dir / "seed.meta").write_text("alembic_head=0008_admin_dashboard\n")
    seed_dir = gate_dir / "seeds" / "0008_admin_dashboard"
    seed_dir.mkdir(parents=True)
    (seed_dir / "seed.dump").write_bytes(b"modern-dump")
    (seed_dir / "seed.meta").write_text("alembic_head=0008_admin_dashboard\n")
    proc = _resolve_seed_files(repo, "0008_admin_dashboard")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SEED_FILE={seed_dir}/seed.dump" in proc.stdout


@needs_tools
def test_a_per_head_directory_missing_its_meta_file_falls_through_to_legacy(
    tmp_path: Path,
) -> None:
    """A half-written per-head directory (e.g. an interrupted --make-seed) must not be
    treated as usable -- both files are required."""
    repo = _make_repo(tmp_path)
    seed_dir = repo / ".gate" / "seeds" / "0008_admin_dashboard"
    seed_dir.mkdir(parents=True)
    (seed_dir / "seed.dump").write_bytes(b"half-written")
    # no seed.meta written
    gate_dir = repo / ".gate"
    (gate_dir / "seed.dump").write_bytes(b"legacy-dump")
    (gate_dir / "seed.meta").write_text("alembic_head=0008_admin_dashboard\n")
    proc = _resolve_seed_files(repo, "0008_admin_dashboard")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SEED_FILE={gate_dir}/seed.dump" in proc.stdout  # legacy, not the half-written dir


@needs_tools
def test_make_seed_only_ever_names_its_own_heads_directory(tmp_path: Path) -> None:
    """Static check on the script text (cheap, no Docker): make_seed() must never write to
    the shared legacy $SEED/$SEED_META constants directly -- only to seed_dir_for_head's own
    per-head directory -- so running --make-seed on one branch can never clobber another
    branch's/head's seed."""
    text = GATE.read_text(encoding="utf-8")
    make_seed_body = text.split("make_seed() {", 1)[1].split("\n}\n", 1)[0]
    assert '>"$SEED.tmp"' not in make_seed_body
    assert '>"$SEED_META"' not in make_seed_body
    assert "seed_dir_for_head" in make_seed_body


# --- global gate lock (T13.14.4, Paralelo-B) --------------------------------------------


@needs_tools
def test_a_second_invocation_queues_behind_the_first_and_prints_the_holder(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    lock_file = tmp_path / "gate.lock"
    holder = _gate_popen(
        repo,
        "--only",
        "git-modes",
        GATE_LOCK_FILE=str(lock_file),
        GATE_LOCK_TEST_SLEEP="2",
    )
    try:
        # The holder file is written right after the lock is taken; poll instead of a
        # fixed sleep so the test isn't racy under a slow/loaded machine.
        deadline = time.time() + 5
        while not (tmp_path / "gate.lock.holder").exists() and time.time() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "gate.lock.holder").exists(), "holder never acquired the lock"

        t_b_start = time.time()
        waiter = _gate_popen(
            repo, "--only", "git-modes", GATE_LOCK_FILE=str(lock_file), GATE_LOCK_TIMEOUT="10"
        )
        out, err = waiter.communicate(timeout=15)
        elapsed = time.time() - t_b_start
        assert waiter.returncode == 0, out + err
        assert "waiting for the gate lock held by" in err
        assert f"pid={holder.pid}" in err, err  # names the real holder, not a placeholder
        assert elapsed >= 1.0, f"the second run finished in {elapsed:.2f}s -- it never queued"
    finally:
        holder.communicate(timeout=10)
    assert holder.returncode == 0


@needs_tools
def test_the_lock_outlives_neither_a_detached_publisher_nor_the_gate_run(
    tmp_path: Path,
) -> None:
    """Regression (found by the T13.14.6 real trial): `publish_gate_status` forks
    scripts/cd/gate_publish.sh, which can itself `setsid nohup … &` a detached publisher
    that waits up to 15 min for the SHA to reach GitHub. That detached process inherits a
    copy of the lock fd (fds are not close-on-exec by default) unless gate.sh explicitly
    closes it first -- otherwise a completed gate run keeps blocking every other
    gate.sh/with-lock.sh run for as long as the detached publisher is alive, long after the
    gate itself printed GATE: PASS and exited. This repo stubs gate_publish.sh with the
    same `setsid nohup … &` shape (no network/gh needed) to prove the fd is closed first."""
    repo = _make_repo(tmp_path)
    (repo / "scripts" / "cd").mkdir(parents=True)
    publisher = repo / "scripts" / "cd" / "gate_publish.sh"
    publisher.write_text(
        "#!/usr/bin/env bash\n" "setsid nohup sleep 30 </dev/null >/dev/null 2>&1 &\n" "exit 0\n",
        encoding="utf-8",
    )
    publisher.chmod(0o755)
    lock_file = tmp_path / "gate.lock"

    proc = _gate(repo, "--only", "git-modes", GATE_LOCK_FILE=str(lock_file))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The detached `sleep 30` must still be alive (proving it really did inherit a copy of
    # whatever fd it got) while a brand-new gate.sh invocation acquires the SAME lock file
    # immediately, with no wait message at all.
    still_detached = subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True, text=True)
    assert still_detached.returncode == 0, "the detached stub exited early -- test is void"
    try:
        proc2 = _gate(repo, "--only", "git-modes", GATE_LOCK_FILE=str(lock_file))
        assert proc2.returncode == 0, proc2.stdout + proc2.stderr
        assert "waiting for the gate lock" not in proc2.stderr
    finally:
        subprocess.run(["pkill", "-f", "sleep 30"], capture_output=True)


@needs_tools
def test_the_lock_is_released_as_soon_as_the_holder_process_group_is_killed(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    lock_file = tmp_path / "gate.lock"
    holder = _gate_popen(
        repo,
        "--only",
        "git-modes",
        GATE_LOCK_FILE=str(lock_file),
        GATE_LOCK_TEST_SLEEP="30",
    )
    deadline = time.time() + 5
    while not (tmp_path / "gate.lock.holder").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert (tmp_path / "gate.lock.holder").exists(), "holder never acquired the lock"

    os.killpg(os.getpgid(holder.pid), signal.SIGKILL)
    holder.wait(timeout=10)

    waiter = _gate_popen(
        repo, "--only", "git-modes", GATE_LOCK_FILE=str(lock_file), GATE_LOCK_TIMEOUT="5"
    )
    out, err = waiter.communicate(timeout=15)
    assert waiter.returncode == 0, out + err
    assert "timed out" not in err  # acquired well inside the 5s timeout, not by exhausting it


@needs_tools
def test_a_waiter_fails_cleanly_when_the_timeout_is_shorter_than_the_hold(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    lock_file = tmp_path / "gate.lock"
    holder = _gate_popen(
        repo,
        "--only",
        "git-modes",
        GATE_LOCK_FILE=str(lock_file),
        GATE_LOCK_TEST_SLEEP="5",
    )
    try:
        deadline = time.time() + 5
        while not (tmp_path / "gate.lock.holder").exists() and time.time() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "gate.lock.holder").exists(), "holder never acquired the lock"

        waiter = _gate_popen(
            repo, "--only", "git-modes", GATE_LOCK_FILE=str(lock_file), GATE_LOCK_TIMEOUT="1"
        )
        out, err = waiter.communicate(timeout=15)
        assert waiter.returncode == 1, out + err
        assert "FAIL: timed out after 1s waiting for the gate lock held by" in err
    finally:
        holder.communicate(timeout=15)
    assert holder.returncode == 0


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


def test_ci_pins_the_same_shellcheck_as_the_local_installer() -> None:
    """F-2026-09-29-1 / DA-C-7: local and CI shellcheck results can only agree on one version."""
    import re

    import yaml

    install = (REPO_ROOT / "scripts" / "prereqs" / "install.sh").read_text(encoding="utf-8")
    local = {
        key: re.search(rf"^{key}=\"([^\"]+)\"", install, re.M).group(1)  # type: ignore[union-attr]
        for key in ("SHELLCHECK_VERSION", "SHELLCHECK_SHA256")
    }
    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["cheap-checks"]["steps"]
    pin = next(s for s in steps if s.get("env", {}).get("SHELLCHECK_SHA256"))
    assert pin["env"] == local
    assert "sha256sum -c -" in pin["run"] and "GITHUB_PATH" in pin["run"]
    gate = next(i for i, s in enumerate(steps) if "gate.sh --only" in s.get("run", ""))
    assert steps.index(pin) < gate


def test_github_actions_are_pinned_to_a_release_on_the_node24_runtime() -> None:
    """DEBT-018: GitHub is removing the Node 20 runtime from hosted runners (Actions started
    refusing node20 actions once the opt-out flag window closed), so every action pinned in
    any workflow must be on a release confirmed (against its own action.yml at the time of
    this fix) to run on node24 -- not just "the next major", in case a future major regresses."""
    import re

    import yaml

    # action -> earliest major confirmed on node24 (checked via each action's action.yml).
    min_node24_major = {
        "actions/checkout": 5,
        "actions/setup-node": 5,
        "actions/setup-python": 6,
        "actions/cache": 5,
        "docker/build-push-action": 7,
        "docker/login-action": 4,
        "docker/setup-buildx-action": 4,
        "azure/login": 3,
        "azure/cli": 3,
        "gitleaks/gitleaks-action": 3,
    }
    found: set[str] = set()
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job in doc.get("jobs", {}).values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                name, _, version = uses.partition("@")
                if name not in min_node24_major:
                    continue
                found.add(name)
                match = re.match(r"v?(\d+)", version)
                assert match, f"{wf.name}: {uses} is not pinned to a major version"
                major = int(match.group(1))
                assert (
                    major >= min_node24_major[name]
                ), f"{wf.name}: {uses} predates node24 (need >= v{min_node24_major[name]})"
    missing = set(min_node24_major) - found
    assert not missing, f"missing from the workflows: {missing}"


def test_ci_caches_and_retries_the_shellcheck_download() -> None:
    """DEBT-025: a transient HTTP 500 from the releases CDN once failed this job outright.
    A cache hit must skip the download entirely; a miss must retry on any failure, not just
    curl's default transient-error list; the checksum check must still run unconditionally."""
    import yaml

    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["cheap-checks"]["steps"]
    cache = next(s for s in steps if s.get("uses", "").startswith("actions/cache@"))
    assert "shellcheck" in cache["with"]["key"]
    download = next(s for s in steps if s.get("env", {}).get("SHELLCHECK_SHA256"))
    assert steps.index(cache) < steps.index(download)
    # The cached path and the downloaded tarball must be the exact same file.
    tarball_var = "shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.xz"
    assert cache["with"]["path"].endswith(tarball_var.replace("${SHELLCHECK_VERSION}", "0.11.0"))
    assert '[ ! -f "$RUNNER_TEMP/$tarball" ]' in download["run"]  # skip the download on a hit
    assert "--retry-all-errors" in download["run"]
    assert "sha256sum -c -" in download["run"]  # verified whether cached or freshly downloaded


# GitHub matches a branch ruleset's required status check by this EXACT job `name:` string
# (main-ci, orchestrator-owned). Renaming any of these five breaks every future merge to
# main until the ruleset is updated too (it was, once: T11c.E3 appended ", private-names)"
# to cheap-checks' name and the orchestrator's merge was rejected — fixed by reverting the
# name, not the ruleset, since nothing here may touch GitHub settings).
REQUIRED_CI_JOB_NAMES = (
    "Backend (lint, types, tests incl. DB)",
    "Frontend (lint, types, build)",
    "Jobs image (build, no torch, pg_dump 16, audit)",
    "Secret scan",
    "Gate cheap checks (git modes, shellcheck, venv, dependency-audit, adr-links)",
)


def test_the_five_required_ci_job_names_exist_verbatim() -> None:
    import yaml

    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    names = {job.get("name") for job in ci["jobs"].values()}
    missing = [name for name in REQUIRED_CI_JOB_NAMES if name not in names]
    assert not missing, (
        f"required CI job name(s) missing or renamed: {missing} — this breaks the main-ci"
        " branch ruleset's required status checks; update the ruleset FIRST (orchestrator +"
        " user, GitHub settings) before renaming a job here"
    )
