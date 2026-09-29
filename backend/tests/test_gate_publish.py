"""The `secrag/gate-full` publisher's decision logic (scripts/cd/gate_publish.sh, DA-C-4).

It publishes only after a --full PASS, for the SHA the gate started on, with HEAD unchanged
and a tree clean at start and end — never otherwise. A fake `gh` (stateful: the commit can
"land" on GitHub after N polls) replaces the real one, in a throwaway git repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISH = REPO_ROOT / "scripts" / "cd" / "gate_publish.sh"
GATE = REPO_ROOT / "scripts" / "gate.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None or shutil.which("setsid") is None,
    reason="needs bash, git and setsid",
)

# Answers `repo view`; `commits/<sha>` fails until it has been asked FAKE_GH_LAND_AFTER
# times (the push has not landed yet); every call is logged.
FAKE_GH = """\
import os, sys
args = " ".join(sys.argv[1:])
log = os.environ["FAKE_GH_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(args + "\\n")
if args.startswith("repo view"):
    print("owner/repo")
    sys.exit(0)
if "-X POST" in args and "/statuses/" in args:
    sys.exit(0)
if "/commits/" in args:
    counter = log + ".polls"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    sys.exit(0 if n >= int(os.environ.get("FAKE_GH_LAND_AFTER", "0")) else 1)
sys.exit(1)
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "a")
    return repo


def _env(tmp_path: Path, land_after: int = 0, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_gh.py"}" "$@"\n')
    gh.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in {"SECRAG_GATE_PUBLISH", "GH_REPO"}}
    return {
        **env,
        "PATH": f"{bin_dir}:{env['PATH']}",
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_LAND_AFTER": str(land_after),
        "GATE_STATUS_POLL_SECONDS": "0.2",
        **extra,
    }


def _publish(
    repo: Path,
    tmp_path: Path,
    *,
    mode: str = "full",
    result: str = "PASS",
    sha: str | None = None,
    start_dirty: str = "0",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(PUBLISH),
            "--mode",
            mode,
            "--result",
            result,
            "--start-sha",
            sha if sha is not None else _git(repo, "rev-parse", "HEAD"),
            "--start-dirty",
            start_dirty,
            "--total",
            "123",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        cwd=repo,
        env=env if env is not None else _env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _posts(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if "-X POST" in line]


def test_a_full_pass_on_a_clean_tree_publishes_for_the_start_sha(repo: Path, tmp_path) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    proc = _publish(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"published secrag/gate-full=success for {sha}" in proc.stdout
    (post,) = _posts(tmp_path)
    assert f"repos/owner/repo/statuses/{sha}" in post and "state=success" in post
    assert "description=scripts/gate.sh --full PASS in 123s" in post


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "fast"}, ""),
        ({"mode": "only"}, ""),
        ({"mode": "seed"}, ""),
        ({"result": "FAIL"}, "NOT published — the gate did not pass"),
        ({"start_dirty": "1"}, "NOT published — the tree had uncommitted"),
        ({"sha": "b" * 40}, "NOT published — HEAD moved during the run"),
        ({"sha": ""}, "NOT published — HEAD moved during the run"),
    ],
)
def test_never_published_otherwise(repo: Path, tmp_path: Path, kwargs, message: str) -> None:
    proc = _publish(repo, tmp_path, **kwargs)
    assert proc.returncode == 0, proc.stderr
    assert message in proc.stdout if message else proc.stdout == ""
    assert _posts(tmp_path) == []
    assert not (tmp_path / "state").exists()  # no background publisher either


def test_a_tree_dirty_at_the_end_is_not_published(repo: Path, tmp_path: Path) -> None:
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    proc = _publish(repo, tmp_path)
    assert "NOT published — the tree has uncommitted or untracked changes" in proc.stdout
    assert _posts(tmp_path) == []


def test_publishing_can_be_switched_off(repo: Path, tmp_path: Path) -> None:
    proc = _publish(repo, tmp_path, env=_env(tmp_path, SECRAG_GATE_PUBLISH="0"))
    assert "not published (SECRAG_GATE_PUBLISH=0)" in proc.stdout
    assert _posts(tmp_path) == []


def test_before_the_push_lands_a_detached_publisher_posts_later(repo: Path, tmp_path) -> None:
    """The pre-push case: the commit reaches GitHub only after the gate; the background
    publisher (a copy of gate_status.sh in the state dir) keeps polling, then posts once."""
    sha = _git(repo, "rev-parse", "HEAD")
    proc = _publish(repo, tmp_path, env=_env(tmp_path, land_after=3))
    assert proc.returncode == 0, proc.stderr
    assert "a background publisher posts secrag/gate-full" in proc.stdout
    assert f"gate_status.sh verify {sha} --wait 300" in proc.stdout  # the DoD's local check
    state = tmp_path / "state"
    assert (state / "gate_status.sh").read_bytes() == (
        REPO_ROOT / "scripts" / "cd" / "gate_status.sh"
    ).read_bytes()
    log = state / "publish-status.log"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and "published" not in log.read_text():
        time.sleep(0.2)
    text = log.read_text()
    assert f"waiting for {sha} on owner/repo" in text
    assert f"published secrag/gate-full=success for {sha}" in text
    (post,) = _posts(tmp_path)
    assert f"statuses/{sha}" in post


def test_gate_hands_every_run_to_the_publisher_with_its_real_result() -> None:
    """gate.sh itself never posts: the decision (mode, PASS/FAIL) is gate_publish.sh's."""
    text = GATE.read_text(encoding="utf-8")
    assert 'publish_gate_status "$mode" "$fail" "$total"' in text
    assert "gate_status.sh publish" not in text
    assert text.count("publish_gate_status ") == 2  # comment + the single call
    fn = text[text.index("publish_gate_status() {") :]
    fn = fn[: fn.index("\n}\n")]
    assert '[ "$fail" -eq 0 ] && result=PASS' in fn and "result=FAIL" in fn
    assert '--start-sha "$GATE_START_SHA" --start-dirty "$GATE_START_DIRTY"' in fn
