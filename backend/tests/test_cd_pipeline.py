"""CD pipeline (T11.0.9 + D-2026-09-27-7 b): cd.yml invariants, the changed-files plan and the
`secrag/gate-full` status check.

`gh` is replaced by a fake that answers per endpoint, so no test talks to GitHub. A route
gives either canned (already filtered) `stdout`, or `json` — a realistic API response to
which the fake applies the script's own `--jq` expression with the real `jq` (DA-C2-2), so
the filters are tested too.
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
AZURE_JOBS = REPO_ROOT / "scripts" / "cd" / "azure_jobs.sh"
SHA = "a" * 40

needs_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)

FAKE_GH = """\
import json, os, subprocess, sys
argv = sys.argv[1:]
args = " ".join(argv)
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(args + "\\n")
for route in json.load(open(os.environ["FAKE_GH_ROUTES"], encoding="utf-8")):
    if route["match"] in args:
        if "json" in route:
            # Like `gh api --jq`: the expression runs on the response, strings print raw.
            expr = argv[argv.index("--jq") + 1] if "--jq" in argv else "."
            proc = subprocess.run(["jq", "-r", expr], input=json.dumps(route["json"]),
                                  capture_output=True, text=True)
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            sys.exit(proc.returncode or route.get("rc", 0))
        sys.stdout.write(route.get("stdout", ""))
        sys.exit(route.get("rc", 0))
sys.exit(1)
"""

# Never skipped in CI (the runner image ships jq): the filter tests must run there.
needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("GITHUB_ACTIONS"),
    reason="needs jq (scripts/prereqs/install.sh jq)",
)


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
    assert {"base_sha", "head_sha", "skip_tip_check"} <= set(inputs)


@pytest.mark.parametrize("workflow", ["cd.yml", "ci.yml"])
def test_no_expression_is_pasted_into_a_run_script(workflow: str) -> None:
    """DA-C2-3: `${{ … }}` in `run:` is template-injected into the shell (a ref name may
    contain `$(…)`); values reach scripts through `env:` instead."""
    wf = yaml.safe_load((CD_YML.parent / workflow).read_text(encoding="utf-8"))
    offenders = [
        f"{job_id}/{step.get('name', step.get('id', '?'))}"
        for job_id, job in wf["jobs"].items()
        for step in job.get("steps", [])
        if "${{" in step.get("run", "")
    ]
    assert offenders == []


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
    assert set(jobs) == {"plan", "gate-status", "dry-run", "images", "migrate", "deploy"}
    assert jobs["gate-status"]["needs"] == ["plan"]
    assert "deploy == 'true'" in jobs["gate-status"]["if"]
    assert jobs["images"]["needs"] == ["plan", "gate-status"]
    # T11.2.6 order: images → migrate Job → apps (+ Job images); a failed migration skips deploy
    assert jobs["migrate"]["needs"] == ["plan", "images"]
    assert jobs["deploy"]["needs"] == ["plan", "images", "migrate"]
    for name in ("images", "migrate", "deploy"):
        assert "real == 'true'" in jobs[name]["if"] and "deploy == 'true'" in jobs[name]["if"]
        assert "always()" not in jobs[name]["if"]  # a refused gate status stops them
    assert "real != 'true'" in jobs["dry-run"]["if"]


def test_the_migrate_job_checks_the_gate_then_runs_the_migration_job_by_digest(cd: dict) -> None:
    job = cd["jobs"]["migrate"]
    runs = [step.get("run", "") for step in job["steps"]]
    verify = [i for i, run in enumerate(runs) if "gate_status.sh verify" in run]
    migrate = [i for i, run in enumerate(runs) if "azure_jobs.sh migrate" in run]
    assert verify and migrate and verify[0] < migrate[0]
    assert job["env"]["JOBS_IMAGE"].endswith("-jobs@${{ needs.images.outputs.jobs_digest }}")
    assert job["env"]["MIGRATE_JOB"] == "${{ vars.AZURE_MIGRATE_JOB }}"
    assert job["permissions"]["id-token"] == "write"


def test_the_deploy_job_updates_the_job_images_after_the_apps(cd: dict) -> None:
    steps = cd["jobs"]["deploy"]["steps"]
    apps = next(i for i, s in enumerate(steps) if "inlineScript" in s.get("with", {}))
    jobs = next(i for i, s in enumerate(steps) if "azure_jobs.sh update-image" in s.get("run", ""))
    assert apps < jobs
    env = steps[jobs]["env"]
    assert env["PURGE_JOB"] == "${{ vars.AZURE_PURGE_JOB }}"
    assert env["BACKUP_JOB"] == "${{ vars.AZURE_BACKUP_JOB }}"
    assert env["JOBS_IMAGE"].endswith("-jobs@${{ needs.images.outputs.jobs_digest }}")


def test_images_build_the_jobs_image(cd: dict) -> None:
    step = next(s for s in cd["jobs"]["images"]["steps"] if s.get("id") == "jobs")
    assert step["with"]["file"] == "./backend/Dockerfile.jobs"
    assert cd["jobs"]["images"]["outputs"]["jobs_digest"] == "${{ steps.jobs.outputs.digest }}"


def test_the_dry_run_shows_the_real_order_and_can_simulate_a_migrate_failure(cd: dict) -> None:
    inputs = _triggers(cd)["workflow_dispatch"]["inputs"]
    assert inputs["simulate_failure"]["default"] is False
    names = [s.get("name", "") for s in cd["jobs"]["dry-run"]["steps"] if "name" in s]
    assert [n.split(".")[0] for n in names] == ["0", "1", "2", "3", "4"]
    assert "Images" in names[1] and "Migration Job" in names[2] and "Apps" in names[3]
    migrate = cd["jobs"]["dry-run"]["steps"][3]
    assert "azure_jobs.sh" in migrate["run"] and "--simulate-failure" in migrate["run"]
    # simulate_failure is refused in real runs, like base_sha/head_sha
    mode = cd["jobs"]["plan"]["steps"][0]["run"]
    assert "SIMULATE_FAILURE" in mode and "dry runs only" in mode


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


def _commands(path: Path) -> str:
    # Comments may mention the rules; commands may not break them (X1).
    text = path.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_cd_never_touches_job_schedules_or_job_yaml() -> None:
    # cd.yml reaches the Jobs only through scripts/cd/azure_jobs.sh.
    assert "containerapp job" not in _commands(CD_YML)
    for path in (CD_YML, AZURE_JOBS):
        commands = _commands(path)
        for forbidden in ("--trigger-type", "--cron-expression", "--yaml", "job create"):
            assert forbidden not in commands, (path.name, forbidden)
    # `job start` never carries per-execution overrides (image/command/env/args).
    starts = [line for line in _commands(AZURE_JOBS).splitlines() if "job start" in line]
    assert starts
    for line in starts:
        for override in ("--image", "--command", "--env-vars", "--args", "--yaml"):
            assert override not in line, line


JOB_YAMLS = sorted((REPO_ROOT / "deploy" / "azure" / "jobs").glob("*.yaml"))


def test_job_definitions_are_manual_single_placeholder_image_and_secretless() -> None:
    """T11.2.6 / X1: versioned Job YAML, applied by hand with a Manual trigger, placeholder
    image (CD sets the digest), parallelism 1, timeout + retry limit, no secret values and no
    API secrets (JobSettings)."""
    assert [p.stem for p in JOB_YAMLS] == ["backup", "migrate", "purge"]
    for path in JOB_YAMLS:
        job = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = job["properties"]["configuration"]
        assert config["triggerType"] == "Manual", path.name
        assert config["manualTriggerConfig"]["parallelism"] == 1
        assert config["replicaTimeout"] > 0 and "replicaRetryLimit" in config
        for secret in config.get("secrets", []):
            assert secret["value"].startswith("<") and secret["value"].endswith(">"), path.name
        (container,) = job["properties"]["template"]["containers"]
        assert "@sha256:" not in container["image"] and "placeholder" in path.read_text()
        env = {e["name"] for e in container["env"]}
        assert "DATABASE_URL" in env and not {"JWT_SECRET", "DATA_MASTER_KEY"} & env
        assert container["resources"]["cpu"] and container["resources"]["memory"]
    migrate = yaml.safe_load((REPO_ROOT / "deploy/azure/jobs/migrate.yaml").read_text())
    assert migrate["properties"]["template"]["containers"][0]["command"] == [
        "alembic",
        "upgrade",
        "head",
    ]


# --- azure_jobs.sh (fake az) ------------------------------------------------------------

FAKE_AZ = """\
import json, os, sys
state_path = os.environ["FAKE_AZ_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"image": "old", "polls": 0}
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
def val(flag):
    return args[args.index(flag) + 1]
cmd = " ".join(a for a in args if not a.startswith("-"))[:40]
if args[:3] == ["containerapp", "job", "update"]:
    if os.environ.get("FAKE_AZ_UPDATE_IGNORED") != "1":
        state["image"] = val("--image")
elif args[:3] == ["containerapp", "job", "show"]:
    print(state["image"])
elif args[:3] == ["containerapp", "job", "start"]:
    print("exec-1")
elif args[:4] == ["containerapp", "job", "execution", "show"]:
    state["polls"] += 1
    seq = os.environ.get("FAKE_AZ_STATUSES", "Running,Succeeded").split(",")
    print(seq[min(state["polls"] - 1, len(seq) - 1)])
else:
    sys.exit(3)
json.dump(state, open(state_path, "w"))
"""

IMAGE = "ghcr.io/o/r-jobs@sha256:" + "b" * 64


def _fake_az(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "azbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_az.py").write_text(FAKE_AZ, encoding="utf-8")
    az = bin_dir / "az"
    az.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_az.py"}" "$@"\n')
    az.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AZ_STATE": str(tmp_path / "az.json"),
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
        "AZURE_JOBS_POLL_SECONDS": "0",
        **extra,
    }


def _az_log(tmp_path: Path) -> list[str]:
    log = tmp_path / "az.log"
    return log.read_text().splitlines() if log.exists() else []


@needs_tools
def test_migrate_updates_by_digest_starts_without_overrides_and_waits(tmp_path: Path) -> None:
    env = _fake_az(tmp_path, FAKE_AZ_STATUSES="Running,Running,Succeeded")
    proc = _run(AZURE_JOBS, "migrate", "--rg", "rg", "--job", "mig", "--image", IMAGE, env=env)
    assert proc.returncode == 0, proc.stderr
    log = _az_log(tmp_path)
    assert log[0].startswith("containerapp job update -n mig -g rg --image " + IMAGE)
    assert log[1].startswith("containerapp job show")
    assert log[2] == "containerapp job start -n mig -g rg --query name -o tsv"
    assert sum("execution show" in line for line in log) == 3
    assert "Succeeded" in proc.stdout


@needs_tools
@pytest.mark.parametrize("status", ["Failed", "Stopped", "Degraded"])
def test_a_failed_migration_stops_the_pipeline(tmp_path: Path, status: str) -> None:
    env = _fake_az(tmp_path, FAKE_AZ_STATUSES=f"Running,{status}")
    proc = _run(AZURE_JOBS, "migrate", "--rg", "rg", "--job", "mig", "--image", IMAGE, env=env)
    assert proc.returncode == 1
    assert status in proc.stderr and "apps are NOT updated" in proc.stderr


@needs_tools
def test_a_migration_that_never_finishes_times_out(tmp_path: Path) -> None:
    env = _fake_az(tmp_path, FAKE_AZ_STATUSES="Running")
    proc = _run(
        AZURE_JOBS,
        "migrate",
        "--rg",
        "rg",
        "--job",
        "mig",
        "--image",
        IMAGE,
        "--timeout",
        "0",
        env=env,
    )
    assert proc.returncode == 1 and "did not finish" in proc.stderr


@needs_tools
def test_an_image_that_did_not_change_fails_before_start(tmp_path: Path) -> None:
    env = _fake_az(tmp_path, FAKE_AZ_UPDATE_IGNORED="1")
    proc = _run(AZURE_JOBS, "migrate", "--rg", "rg", "--job", "mig", "--image", IMAGE, env=env)
    assert proc.returncode == 1 and "expected" in proc.stderr
    assert not any("job start" in line for line in _az_log(tmp_path))


@needs_tools
@pytest.mark.parametrize("image", ["ghcr.io/o/r-jobs:latest", "ghcr.io/o/r-jobs:abc", ""])
def test_an_image_without_a_digest_is_refused(tmp_path: Path, image: str) -> None:
    env = _fake_az(tmp_path)
    proc = _run(AZURE_JOBS, "update-image", "--rg", "rg", "--job", "p", "--image", image, env=env)
    assert proc.returncode != 0 and _az_log(tmp_path) == []


@needs_tools
def test_update_image_never_starts_the_job(tmp_path: Path) -> None:
    env = _fake_az(tmp_path)
    proc = _run(AZURE_JOBS, "update-image", "--rg", "rg", "--job", "p", "--image", IMAGE, env=env)
    assert proc.returncode == 0, proc.stderr
    assert [line.split()[2] for line in _az_log(tmp_path)] == ["update", "show"]


@needs_tools
def test_dry_run_prints_the_order_and_simulates_a_failure(tmp_path: Path) -> None:
    env = _fake_az(tmp_path)
    args = ("migrate", "--rg", "rg", "--job", "m", "--image", IMAGE)
    ok = _run(AZURE_JOBS, *args, "--dry-run", env=env)
    assert ok.returncode == 0
    lines = [line for line in ok.stdout.splitlines() if "would run" in line]
    assert "job update" in lines[0] and "job start" in lines[1]
    bad = _run(
        AZURE_JOBS,
        "migrate",
        "--rg",
        "rg",
        "--job",
        "m",
        "--image",
        IMAGE,
        "--dry-run",
        "--simulate-failure",
        env=env,
    )
    assert bad.returncode == 1 and "simulated" in bad.stderr
    assert _az_log(tmp_path) == []  # a dry run never calls az
    real = _run(
        AZURE_JOBS,
        "migrate",
        "--rg",
        "rg",
        "--job",
        "m",
        "--image",
        IMAGE,
        "--simulate-failure",
        env=env,
    )
    assert real.returncode == 1 and "dry runs only" in real.stderr


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


# DA-C2-1: an unreadable Deployments API must never turn into "nothing deployed yet →
# deploy everything" (that would skip the ancestor/equal and docs-only checks).
@needs_tools
@pytest.mark.parametrize(
    "routes",
    [
        [{"match": "deployments?environment=azure", "stdout": "", "rc": 1}],
        [{"match": "deployments?environment=azure", "stdout": "HTTP 502 Bad Gateway\n"}],
        [
            {"match": "deployments/7/statuses", "stdout": "", "rc": 1},
            {"match": "deployments?environment=azure", "stdout": f"7:{'b' * 40}\n"},
        ],
    ],
    ids=["list-fails", "list-garbage", "statuses-fail"],
)
def test_a_deployments_api_error_refuses_to_plan(tmp_path: Path, history, routes) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], routes=routes)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "cannot read the GitHub deployments" in proc.stderr
    assert "deploy" not in out


@needs_tools
@needs_jq
def test_the_deployments_jq_filters_pick_the_newest_success(tmp_path: Path, history) -> None:
    repo, shas = history
    routes = [
        {"match": "deployments/9/statuses", "json": [{"state": "failure", "id": 91}]},
        {"match": "deployments/8/statuses", "json": []},
        {
            "match": "deployments/7/statuses",
            "json": [{"state": "success", "id": 71}, {"state": "in_progress", "id": 70}],
        },
        {
            "match": "deployments?environment=azure",
            "json": [
                {"id": 9, "sha": shas["code"], "environment": "azure", "ref": "main"},
                {"id": 8, "sha": shas["docs"], "environment": "azure", "ref": "main"},
                {"id": 7, "sha": shas["base"], "environment": "azure", "ref": "main"},
            ],
        },
    ]
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], routes=routes)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["base"] == shas["base"]
    assert out["deploy"] == "true"


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


# --- plan.sh: never roll back, never deploy a superseded commit (DA-C-1) ----------------


@needs_tools
def test_an_ancestor_of_the_deployed_sha_is_never_deployed(tmp_path: Path, history) -> None:
    """CI of an OLD commit re-run after a newer one was deployed: the diff is non-empty,
    but deploying it would roll back (older code on a newer schema)."""
    repo, shas = history
    for older in ("base", "docs"):
        proc, out = _plan(tmp_path, repo, "--sha", shas[older], "--base", shas["code"])
        assert proc.returncode == 0, proc.stderr
        assert out["deploy"] == "false", proc.stdout
        assert "older than the deployed" in out["reason"]
        assert "never rolls back" in out["reason"]


@needs_tools
def test_an_ancestor_of_the_last_deployment_from_the_api_is_skipped(
    tmp_path: Path, history
) -> None:
    repo, shas = history
    routes = [
        {"match": "deployments/5/statuses", "stdout": "success\n"},
        {"match": "deployments?environment=azure", "stdout": f"5:{shas['code']}\n"},
    ]
    proc, out = _plan(tmp_path, repo, "--sha", shas["base"], routes=routes)
    assert out["base"] == shas["code"] and out["deploy"] == "false", proc.stdout


@needs_tools
def test_a_sha_that_is_no_longer_the_tip_is_skipped(tmp_path: Path, history) -> None:
    """A newer commit landed after this CI run: its own CD run deploys it."""
    repo, shas = history
    proc, out = _plan(
        tmp_path, repo, "--sha", shas["docs"], "--base", shas["base"], "--tip", shas["code"]
    )
    assert proc.returncode == 0, proc.stderr
    assert out["deploy"] == "false"
    assert "no longer the branch tip" in out["reason"]


@needs_tools
def test_the_tip_itself_is_planned_normally(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(
        tmp_path, repo, "--sha", shas["code"], "--base", shas["docs"], "--tip", shas["code"]
    )
    assert out["deploy"] == "true", proc.stdout
    # Tip given, nothing deployed yet: the tip check passes, then "deploy everything".
    routes = [{"match": "deployments?environment=azure", "stdout": ""}]
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], "--tip", shas["code"], routes=routes)
    assert out["deploy"] == "true" and "no successful deployment" in out["reason"], proc.stdout


@needs_tools
@pytest.mark.parametrize("tip", ["", "main", "abc"])
def test_an_unknown_tip_refuses_to_plan(tmp_path: Path, history, tip: str) -> None:
    """ls-remote failed → empty tip → the plan job fails (fail-closed), no deploy output."""
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], "--base", shas["docs"], "--tip", tip)
    assert proc.returncode == 1
    assert "deploy" not in out


@needs_tools
def test_a_diverged_sha_is_planned_by_its_diff(tmp_path: Path, history) -> None:
    """Neither ancestor nor descendant (e.g. history rewritten): the diff decides."""
    repo, shas = history
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", shas["base"]], check=True)
    side = _commit(repo, {"backend/other.py": "x"}, "side")
    proc, out = _plan(tmp_path, repo, "--sha", side, "--base", shas["code"], "--tip", side)
    assert out["deploy"] == "true", proc.stdout


def test_cd_passes_the_live_tip_to_the_plan(cd: dict) -> None:
    steps = cd["jobs"]["plan"]["steps"]
    tip = next(s for s in steps if s.get("id") == "tip")
    assert "git ls-remote --exit-code origin" in tip["run"]
    assert "refs/heads/main" in tip["env"]["REF"]  # workflow_run: always main's head
    assert tip["if"] == "inputs.skip_tip_check != true"
    plan = next(s for s in steps if s.get("id") == "plan")
    assert "--tip" in plan["run"]
    assert plan["env"]["CHECK_TIP"] == "${{ steps.tip.conclusion == 'success' }}"
    mode = next(s for s in steps if s.get("id") == "mode")
    # A real run can never switch the tip check off.
    assert '[ "$SKIP_TIP_CHECK" = true ]' in mode["run"] and "exit 1" in mode["run"]
    inputs = _triggers(cd)["workflow_dispatch"]["inputs"]
    assert inputs["skip_tip_check"]["default"] is False


# --- ci_status.sh + manual deploys need CI (DA-C-2, D-2026-09-29-1 a) -------------------

CI_STATUS = REPO_ROOT / "scripts" / "cd" / "ci_status.sh"


@needs_tools
@pytest.mark.parametrize(
    ("stdout", "rc", "expected"),
    [
        ("1\n", 0, "CI requirement met"),
        ("2\n", 0, "CI requirement met"),
        ("0\n", 1, "REFUSED — no successful ci.yml push run"),
        ("\n", 1, "REFUSED"),
        ("null\n", 1, "REFUSED"),
    ],
)
def test_ci_status(tmp_path: Path, stdout: str, rc: int, expected: str) -> None:
    env = _fake_gh(tmp_path, [{"match": "actions/workflows/ci.yml/runs", "stdout": stdout}])
    proc = _run(CI_STATUS, "verify", SHA, env=env)
    assert proc.returncode == rc, proc.stdout + proc.stderr
    assert expected in proc.stdout
    call = (tmp_path / "gh.log").read_text(encoding="utf-8")
    for part in (f"head_sha={SHA}", "branch=main", "event=push", "status=success"):
        assert part in call
    # The jq filter re-checks every field (a query parameter the API ignored cannot widen it).
    for field in (
        f'.head_sha == "{SHA}"',
        '.head_branch == "main"',
        '.event == "push"',
        '.conclusion == "success"',
    ):
        assert field in call


def _run_json(**overrides: object) -> dict[str, object]:
    """A workflow run as the Actions API returns it (trimmed to the fields that matter)."""
    run: dict[str, object] = {
        "id": 36617249764,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "head_branch": "main",
        "head_sha": SHA,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
    }
    run.update(overrides)
    return run


# DA-C2-2: the jq filter itself, on realistic API JSON. The API's query parameters are
# assumed to be IGNORED here (every run is returned), so only the filter decides.
@needs_tools
@needs_jq
@pytest.mark.parametrize(
    ("runs", "rc", "expected"),
    [
        ([_run_json()], 0, "(1 run(s)) — CI requirement met"),
        ([_run_json(), _run_json(id=2, run_attempt=2)], 0, "(2 run(s))"),
        ([_run_json(head_sha="b" * 40)], 1, "REFUSED"),
        ([_run_json(event="pull_request")], 1, "REFUSED"),
        ([_run_json(head_branch="phase-11a-persistence")], 1, "REFUSED"),
        ([_run_json(conclusion="failure")], 1, "REFUSED"),
        ([_run_json(conclusion=None, status="in_progress")], 1, "REFUSED"),
        (
            [_run_json(event="pull_request"), _run_json(conclusion="failure"), _run_json()],
            0,
            "(1 run(s))",
        ),
        ([], 1, "REFUSED"),
    ],
    ids=[
        "success",
        "two-attempts",
        "other-sha",
        "pull-request",
        "other-branch",
        "failure",
        "in-progress",
        "mixed",
        "none",
    ],
)
def test_ci_status_filter_on_api_json(tmp_path: Path, runs: list, rc: int, expected: str) -> None:
    body = {"total_count": len(runs), "workflow_runs": runs}
    env = _fake_gh(tmp_path, [{"match": "actions/workflows/ci.yml/runs", "json": body}])
    proc = _run(CI_STATUS, "verify", SHA, env=env)
    assert proc.returncode == rc, proc.stdout + proc.stderr
    assert expected in proc.stdout


@needs_tools
def test_ci_status_refuses_on_an_api_error(tmp_path: Path) -> None:
    env = _fake_gh(tmp_path, [{"match": "actions/workflows", "stdout": "", "rc": 1}])
    proc = _run(CI_STATUS, "verify", SHA, "--branch", "main", env=env)
    assert proc.returncode == 1 and "API error" in proc.stdout


@needs_tools
@pytest.mark.parametrize("args", [["verify", "abc"], ["check", SHA], ["verify"]])
def test_ci_status_usage_errors(tmp_path: Path, args: list[str]) -> None:
    assert _run(CI_STATUS, *args, env=_fake_gh(tmp_path, [])).returncode == 2


def test_a_real_manual_deploy_requires_ci_success(cd: dict) -> None:
    plan = cd["jobs"]["plan"]
    assert plan["permissions"]["actions"] == "read"
    steps = plan["steps"]
    ci = next(s for s in steps if "ci_status.sh verify" in s.get("run", ""))
    assert ci["if"] == "github.event_name == 'workflow_dispatch'"
    assert "--branch main" in ci["run"]
    # Real → the step (so the plan job, so every later job) fails.
    assert 'if [ "$REAL" = true ]; then' in ci["run"] and "exit 1" in ci["run"]
    plan_step = next(s for s in steps if s.get("id") == "plan")
    assert steps.index(ci) < steps.index(plan_step)
