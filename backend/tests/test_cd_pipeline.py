"""CD pipeline (T11.0.9 + D-2026-09-27-7 b): cd.yml invariants, the changed-files plan and the
`secrag/gate-full` status check.

`gh` is replaced by a fake that answers per endpoint with canned (already jq-filtered)
output, so no test talks to GitHub. The jq filters themselves are exercised by the real
dry-run dispatches recorded in PHASE_STATUS.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CD_YML = REPO_ROOT / ".github" / "workflows" / "cd.yml"
GATE_STATUS = REPO_ROOT / "scripts" / "cd" / "gate_status.sh"
PLAN = REPO_ROOT / "scripts" / "cd" / "plan.sh"
SHA = "a" * 40

needs_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)

FAKE_GH = """\
import json, os, sys
args = " ".join(sys.argv[1:])
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(args + "\\n")
for route in json.load(open(os.environ["FAKE_GH_ROUTES"], encoding="utf-8")):
    if route["match"] in args:
        sys.stdout.write(route.get("stdout", ""))
        sys.exit(route.get("rc", 0))
sys.exit(1)
"""


def _fake_gh(tmp_path: Path, routes: list[dict[str, object]]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_gh.py"}" "$@"\n')
    gh.chmod(0o755)
    (tmp_path / "routes.json").write_text(json.dumps(routes), encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_ROUTES": str(tmp_path / "routes.json"),
        "GH_REPO": "owner/repo",
        "GATE_STATUS_POLL_SECONDS": "0",
    }


def _run(script: Path, *args: str, env: dict[str, str], cwd: Path | None = None):
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- cd.yml invariants ------------------------------------------------------------------


@pytest.fixture(scope="module")
def cd() -> dict:
    return yaml.safe_load(CD_YML.read_text(encoding="utf-8"))


def _triggers(cd: dict) -> dict:
    return cd.get("on") or cd[True]  # YAML 1.1 reads the bare key `on` as True


def test_cd_runs_after_ci_on_main_and_never_on_a_raw_push(cd: dict) -> None:
    triggers = _triggers(cd)
    assert "push" not in triggers
    assert triggers["workflow_run"] == {
        "workflows": ["CI"],
        "types": ["completed"],
        "branches": ["main"],
    }
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True
    assert {"base_sha", "head_sha"} <= set(inputs)


def test_no_job_uses_environment_so_the_oidc_subject_stays_main(cd: dict) -> None:
    assert all("environment" not in job for job in cd["jobs"].values())


def test_concurrency_keeps_real_deploys_in_one_uncancelled_group(cd: dict) -> None:
    group = cd["concurrency"]["group"]
    assert "'cd-main'" in group and "cd-noop-{0}" in group
    assert cd["concurrency"]["cancel-in-progress"] is False


def test_plan_uses_the_workflow_scripts_not_the_planned_commit(cd: dict) -> None:
    """Found by a dry run: an older head_sha has no scripts/cd, so it must not be checked out."""
    checkout = next(
        s for s in cd["jobs"]["plan"]["steps"] if "actions/checkout" in s.get("uses", "")
    )
    assert checkout["with"] == {"fetch-depth": 0}


def test_real_jobs_need_the_plan_and_the_gate_status(cd: dict) -> None:
    jobs = cd["jobs"]
    assert set(jobs) == {"plan", "gate-status", "dry-run", "images", "deploy"}
    assert jobs["gate-status"]["needs"] == ["plan"]
    assert "deploy == 'true'" in jobs["gate-status"]["if"]
    assert jobs["images"]["needs"] == ["plan", "gate-status"]
    assert jobs["deploy"]["needs"] == ["plan", "images"]
    for name in ("images", "deploy"):
        assert "real == 'true'" in jobs[name]["if"] and "deploy == 'true'" in jobs[name]["if"]
        assert "always()" not in jobs[name]["if"]  # a refused gate status stops them
    assert "real != 'true'" in jobs["dry-run"]["if"]


def test_deploy_job_refuses_without_gate_status_and_deploys_by_digest(cd: dict) -> None:
    deploy = cd["jobs"]["deploy"]
    runs = [step.get("run", "") for step in deploy["steps"]]
    verify = [i for i, run in enumerate(runs) if "gate_status.sh verify" in run]
    record = [i for i, run in enumerate(runs) if "repos/$REPO/deployments" in run]
    assert verify and record and verify[0] < record[0]
    script = next(
        s["with"]["inlineScript"]
        for s in deploy["steps"]
        if "with" in s and "inlineScript" in s["with"]
    )
    assert "-backend@${{ needs.images.outputs.backend_digest }}" in script
    assert "-frontend@${{ needs.images.outputs.frontend_digest }}" in script
    assert deploy["permissions"]["deployments"] == "write"
    assert deploy["permissions"]["id-token"] == "write"


def test_cd_never_touches_job_schedules_or_job_yaml() -> None:
    text = CD_YML.read_text(encoding="utf-8")
    # Comments may mention the rule; commands may not exist (X1).
    commands = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("containerapp job", "--trigger-type", "--cron-expression", "--yaml"):
        assert forbidden not in commands


# --- gate_status.sh ---------------------------------------------------------------------


@needs_tools
@pytest.mark.parametrize(
    ("latest", "creator", "rc", "expected"),
    [
        ("success owner", "owner", 0, "deploy allowed"),
        ("success owner", "", 0, "deploy allowed"),
        (" ", "owner", 1, "REFUSED — no secrag/gate-full status"),
        ("failure owner", "owner", 1, "REFUSED — secrag/gate-full is 'failure'"),
        ("success github-actions[bot]", "owner", 1, "posted by 'github-actions[bot]'"),
    ],
)
def test_verify(tmp_path: Path, latest: str, creator: str, rc: int, expected: str) -> None:
    env = _fake_gh(tmp_path, [{"match": f"commits/{SHA}/statuses", "stdout": latest + "\n"}])
    args = ["verify", SHA, *(["--creator", creator] if creator else [])]
    proc = _run(GATE_STATUS, *args, env=env)
    assert proc.returncode == rc, proc.stdout + proc.stderr
    assert expected in proc.stdout


@needs_tools
def test_verify_waits_for_a_late_status(tmp_path: Path) -> None:
    """With --wait the check polls: a background publisher may post a little later."""
    env = _fake_gh(tmp_path, [{"match": "statuses", "stdout": " \n"}])
    proc = _run(
        GATE_STATUS, "verify", SHA, "--wait", "1", env={**env, "GATE_STATUS_POLL_SECONDS": "0.3"}
    )
    assert proc.returncode == 1
    polls = (tmp_path / "gh.log").read_text(encoding="utf-8").count("statuses")
    assert polls >= 2


@needs_tools
def test_publish_posts_success_for_the_exact_sha(tmp_path: Path) -> None:
    env = _fake_gh(
        tmp_path,
        [
            {"match": f"-X POST repos/owner/repo/statuses/{SHA}", "rc": 0},
            {"match": f"repos/owner/repo/commits/{SHA}", "rc": 0},
        ],
    )
    proc = _run(GATE_STATUS, "publish", SHA, "--description", "gate PASS in 176s", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    post = [line for line in (tmp_path / "gh.log").read_text().splitlines() if "-X POST" in line]
    assert len(post) == 1
    assert "state=success" in post[0] and "context=secrag/gate-full" in post[0]
    assert "description=gate PASS in 176s" in post[0]


@needs_tools
def test_publish_reports_a_commit_not_on_github_yet(tmp_path: Path) -> None:
    env = _fake_gh(tmp_path, [{"match": f"commits/{SHA}", "rc": 1}])
    proc = _run(GATE_STATUS, "publish", SHA, env=env)
    assert proc.returncode == 3
    assert "-X POST" not in (tmp_path / "gh.log").read_text()


@needs_tools
@pytest.mark.parametrize("bad", ["abc", "A" * 40, "HEAD"])
def test_only_full_shas_are_accepted(tmp_path: Path, bad: str) -> None:
    proc = _run(GATE_STATUS, "verify", bad, env=_fake_gh(tmp_path, []))
    assert proc.returncode == 2


# --- plan.sh ----------------------------------------------------------------------------


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shas = {
        "base": _commit(repo, {"backend/app.py": "1", "README.md": "a"}, "base"),
    }
    shas["docs"] = _commit(
        repo, {"docs/x.md": "d", "README.md": "b", "deploy/k8s/a.yaml": "k"}, "docs"
    )
    shas["code"] = _commit(repo, {"backend/app.py": "2"}, "code")
    return repo, shas


def _plan(tmp_path: Path, repo: Path, *args: str, routes: list | None = None):
    env = _fake_gh(tmp_path, routes or [])
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    proc = _run(PLAN, *args, env={**env, "GITHUB_OUTPUT": str(out)}, cwd=repo)
    outputs = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    return proc, outputs


@needs_tools
def test_docs_only_change_skips_build_and_deploy(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["docs"], "--base", shas["base"])
    assert proc.returncode == 0, proc.stderr
    assert out["deploy"] == "false"
    assert out["sha"] == shas["docs"] and out["base"] == shas["base"]
    assert "only docs/**" in out["reason"]


@needs_tools
def test_code_change_deploys(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], "--base", shas["docs"])
    assert out["deploy"] == "true", proc.stdout
    assert "backend/app.py" in out["reason"]


@needs_tools
def test_no_recorded_deployment_deploys_everything(tmp_path: Path, history) -> None:
    repo, shas = history
    routes = [{"match": "deployments?environment=azure", "stdout": ""}]
    proc, out = _plan(tmp_path, repo, "--sha", shas["docs"], routes=routes)
    assert out["deploy"] == "true", proc.stdout
    assert "no successful deployment" in out["reason"]


@needs_tools
def test_base_is_the_newest_successful_deployment(tmp_path: Path, history) -> None:
    repo, shas = history
    routes = [
        {"match": "deployments/9/statuses", "stdout": "failure\n"},
        {"match": "deployments/7/statuses", "stdout": "success\n"},
        {
            "match": "deployments?environment=azure",
            "stdout": f"9:{shas['code']}\n7:{shas['base']}\n",
        },
    ]
    proc, out = _plan(tmp_path, repo, "--sha", shas["docs"], routes=routes)
    assert out["base"] == shas["base"], proc.stdout
    assert out["deploy"] == "false"


@needs_tools
def test_same_sha_as_deployed_skips(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], "--base", shas["code"])
    assert out["deploy"] == "false" and "nothing changed" in out["reason"], proc.stdout


@needs_tools
def test_unknown_base_deploys_everything(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["docs"], "--base", "b" * 40)
    assert out["deploy"] == "true", proc.stdout
    assert "not in this clone" in out["reason"]
