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
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CD_YML = REPO_ROOT / ".github" / "workflows" / "cd.yml"
GATE_STATUS = REPO_ROOT / "scripts" / "cd" / "gate_status.sh"
PLAN = REPO_ROOT / "scripts" / "cd" / "plan.sh"
AZURE_JOBS = REPO_ROOT / "scripts" / "cd" / "azure_jobs.sh"
HEALTH_CHECK = REPO_ROOT / "scripts" / "cd" / "health_check.sh"
RESOLVE_IMAGE = REPO_ROOT / "scripts" / "cd" / "resolve_image.sh"
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


def test_force_all_input_exists_defaults_false_and_is_allowed_on_real_runs(cd: dict) -> None:
    """T13.CD.1: unlike base_sha/head_sha/skip_tip_check/simulate_failure, force_all works on
    BOTH dry runs and real runs — it must never be refused by the "dry runs only" guard."""
    inputs = _triggers(cd)["workflow_dispatch"]["inputs"]
    assert inputs["force_all"]["type"] == "boolean"
    assert inputs["force_all"]["default"] is False
    mode = cd["jobs"]["plan"]["steps"][0]["run"]
    assert "FORCE_ALL" not in mode  # never part of the dry-runs-only refusal check
    plan_step = next(s for s in cd["jobs"]["plan"]["steps"] if s.get("id") == "plan")
    assert plan_step["env"]["FORCE_ALL"] == "${{ inputs.force_all }}"
    assert "--force-all" in plan_step["run"]


SCRIPT_LIKE_WITH_KEYS = ("inlineScript", "script")


@pytest.mark.parametrize("workflow", ["cd.yml", "ci.yml"])
def test_no_expression_is_pasted_into_a_run_script(workflow: str) -> None:
    """DA-C2-3 / DA-13-0-2: `${{ … }}` in `run:` — or in a script-like `with:` key such as
    azure/cli@v3's `inlineScript` — is template-injected into the shell (a ref name or a
    digest could in principle contain shell metacharacters); values reach scripts through
    `env:` instead."""
    wf = yaml.safe_load((CD_YML.parent / workflow).read_text(encoding="utf-8"))
    offenders = [
        f"{job_id}/{step.get('name', step.get('id', '?'))}"
        for job_id, job in wf["jobs"].items()
        for step in job.get("steps", [])
        if "${{" in step.get("run", "")
        or any("${{" in step.get("with", {}).get(key, "") for key in SCRIPT_LIKE_WITH_KEYS)
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
    resolve = [i for i, run in enumerate(runs) if "resolve_image.sh job" in run]
    migrate = [i for i, run in enumerate(runs) if "azure_jobs.sh migrate" in run]
    assert verify and resolve and migrate and verify[0] < resolve[0] == migrate[0]
    assert job["env"]["MIGRATE_JOB"] == "${{ vars.AZURE_MIGRATE_JOB }}"
    assert job["env"]["BUILD_JOBS"] == "${{ needs.plan.outputs.build_jobs }}"
    assert job["env"]["JOBS_BUILT_DIGEST"] == "${{ needs.images.outputs.jobs_digest }}"
    assert "JOBS_IMAGE" not in job["env"]  # resolved dynamically, not a static digest
    assert job["permissions"]["id-token"] == "write"


def test_the_migrate_job_resolve_call_passes_the_build_flag_and_built_digest(cd: dict) -> None:
    """T13.CD.2: the jobs image is either the one just built (BUILD_JOBS=true) or the one
    currently deployed on the migration Job itself, fail-closed if unreadable/unpinned."""
    job = cd["jobs"]["migrate"]
    run = next(s["run"] for s in job["steps"] if "resolve_image.sh job" in s.get("run", ""))
    assert '--rg "$RESOURCE_GROUP"' in run and '--name "$MIGRATE_JOB"' in run
    assert '--build "$BUILD_JOBS"' in run and '--built-digest "$JOBS_BUILT_DIGEST"' in run
    assert '--repo "ghcr.io/$GH_REPO-jobs"' in run


def test_the_migrate_job_exposes_its_resolved_jobs_image_as_an_output(cd: dict) -> None:
    """DA-13-0-1: `deploy` needs this to cross-check the purge/backup Jobs when build_jobs
    stayed false — the resolved image must be captured, not just consumed locally."""
    job = cd["jobs"]["migrate"]
    assert job["outputs"]["jobs_image"] == "${{ steps.resolve.outputs.jobs_image }}"
    resolve = next(s for s in job["steps"] if "resolve_image.sh job" in s.get("run", ""))
    assert resolve.get("id") == "resolve"
    assert 'echo "jobs_image=$JOBS_IMAGE" >> "$GITHUB_OUTPUT"' in resolve["run"]


def test_the_deploy_job_updates_the_job_images_after_the_apps(cd: dict) -> None:
    steps = cd["jobs"]["deploy"]["steps"]
    apps = next(i for i, s in enumerate(steps) if "inlineScript" in s.get("with", {}))
    jobs = next(i for i, s in enumerate(steps) if "azure_jobs.sh update-image" in s.get("run", ""))
    assert apps < jobs
    env = steps[jobs]["env"]
    assert env["PURGE_JOB"] == "${{ vars.AZURE_PURGE_JOB }}"
    assert env["BACKUP_JOB"] == "${{ vars.AZURE_BACKUP_JOB }}"
    assert env["JOBS_IMAGE"].endswith("-jobs@${{ needs.images.outputs.jobs_digest }}")
    # T13.CD.2: unchanged Job images are never touched (migrate already resolved/verified
    # the kept digest this same run; no reason to call az again here).
    assert steps[jobs]["if"] == "needs.plan.outputs.build_jobs == 'true'"


def _cross_check_step(cd: dict) -> dict:
    return next(
        s
        for s in cd["jobs"]["deploy"]["steps"]
        if "resolve_image.sh job" in s.get("run", "") and "update-image" not in s.get("run", "")
    )


def test_the_deploy_job_cross_checks_job_images_when_build_jobs_is_false(cd: dict) -> None:
    """DA-13-0-1: when build_jobs stayed false, nothing else in this run re-reads the
    purge/backup Jobs' own currently deployed image — this step fails closed on drift
    (e.g. an out-of-band `job update` applied by hand to only one of the three Jobs during
    an incident), instead of trusting the ADR's "they always move together" assumption."""
    deploy = cd["jobs"]["deploy"]
    update_step = next(
        s for s in deploy["steps"] if "azure_jobs.sh update-image" in s.get("run", "")
    )
    cross_check = _cross_check_step(cd)
    assert update_step["if"] == "needs.plan.outputs.build_jobs == 'true'"
    assert cross_check["if"] == "needs.plan.outputs.build_jobs != 'true'"
    assert cross_check["env"]["MIGRATE_IMAGE"] == "${{ needs.migrate.outputs.jobs_image }}"
    assert cross_check["env"]["PURGE_JOB"] == "${{ vars.AZURE_PURGE_JOB }}"
    assert cross_check["env"]["BACKUP_JOB"] == "${{ vars.AZURE_BACKUP_JOB }}"
    assert "--build false" in cross_check["run"]
    assert "::error::" in cross_check["run"] and "no auto-repair" in cross_check["run"]
    # No auto-repair: the step never calls `job update` or `azure_jobs.sh update-image`.
    assert "update-image" not in cross_check["run"] and "job update" not in cross_check["run"]
    assert cd["jobs"]["deploy"]["needs"] == ["plan", "images", "migrate"]  # jobs_image output


def test_images_build_the_jobs_image(cd: dict) -> None:
    step = next(s for s in cd["jobs"]["images"]["steps"] if s.get("id") == "jobs")
    assert step["with"]["file"] == "./backend/Dockerfile.jobs"
    assert cd["jobs"]["images"]["outputs"]["jobs_digest"] == "${{ steps.jobs.outputs.digest }}"


def test_images_build_the_ollama_image_with_a_gha_cache(cd: dict) -> None:
    """T11.4.3 + DA-11bC-1: the custom Ollama image is built from its own Dockerfile and
    cached the same way the other three images are."""
    step = next(s for s in cd["jobs"]["images"]["steps"] if s.get("id") == "ollama")
    assert step["with"]["context"] == "./ollama"
    assert step["with"]["file"] == "./ollama/Dockerfile"
    assert "type=gha" in step["with"]["cache-from"]
    assert "type=gha" in step["with"]["cache-to"]
    assert cd["jobs"]["images"]["outputs"]["ollama_digest"] == "${{ steps.ollama.outputs.digest }}"
    steps = cd["jobs"]["images"]["steps"]
    assert any(s.get("uses", "").startswith("docker/setup-buildx-action") for s in steps)


def test_each_image_build_step_only_runs_when_the_plan_flagged_it(cd: dict) -> None:
    """T13.CD.2: only the images the plan flagged are built; an unchanged image's build step
    is skipped — its digest is resolved from Azure instead (migrate/deploy jobs)."""
    steps = cd["jobs"]["images"]["steps"]
    expected = {
        "backend": "needs.plan.outputs.build_backend == 'true'",
        "frontend": "needs.plan.outputs.build_frontend == 'true'",
        "jobs": "needs.plan.outputs.build_jobs == 'true'",
        "ollama": "needs.plan.outputs.build_ollama == 'true'",
    }
    for step_id, expected_if in expected.items():
        step = next(s for s in steps if s.get("id") == step_id)
        assert step["if"] == expected_if, step_id
    login_and_setup = [
        s for s in steps if s.get("uses", "").startswith(("docker/setup-buildx", "docker/login"))
    ]
    assert login_and_setup and all("if" not in s for s in login_and_setup)


def test_the_dry_run_shows_the_real_order_and_can_simulate_a_migrate_failure(cd: dict) -> None:
    inputs = _triggers(cd)["workflow_dispatch"]["inputs"]
    assert inputs["simulate_failure"]["default"] is False
    names = [s.get("name", "") for s in cd["jobs"]["dry-run"]["steps"] if "name" in s]
    assert [n.split(".")[0] for n in names] == ["0", "1", "2", "3", "4", "5"]
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
    apps_step = next(s for s in deploy["steps"] if "with" in s and "inlineScript" in s["with"])
    script = apps_step["with"]["inlineScript"]
    # T13.CD.2: the digest itself is resolved in an earlier step (built just now, or kept
    # from Azure). DA-13-0-2: it reaches the inlineScript through `env:`, never pasted as a
    # `${{ }}` expression into the script text itself.
    assert "${{" not in script
    assert apps_step["env"]["BACKEND_IMAGE"] == "${{ steps.resolve.outputs.backend_image }}"
    assert apps_step["env"]["FRONTEND_IMAGE"] == "${{ steps.resolve.outputs.frontend_image }}"
    assert apps_step["env"]["OLLAMA_IMAGE"] == "${{ steps.resolve.outputs.ollama_image }}"
    assert "$BACKEND_IMAGE" in script and "$FRONTEND_IMAGE" in script and "$OLLAMA_IMAGE" in script
    resolve = next(s for s in deploy["steps"] if s.get("id") == "resolve")
    assert resolve["env"]["BACKEND_BUILT_DIGEST"] == "${{ needs.images.outputs.backend_digest }}"
    assert resolve["env"]["FRONTEND_BUILT_DIGEST"] == "${{ needs.images.outputs.frontend_digest }}"
    assert resolve["env"]["OLLAMA_BUILT_DIGEST"] == "${{ needs.images.outputs.ollama_digest }}"
    assert resolve["env"]["BUILD_BACKEND"] == "${{ needs.plan.outputs.build_backend }}"
    assert resolve["env"]["BUILD_FRONTEND"] == "${{ needs.plan.outputs.build_frontend }}"
    assert resolve["env"]["BUILD_OLLAMA"] == "${{ needs.plan.outputs.build_ollama }}"
    assert "resolve_image.sh app" in resolve["run"]
    pin_check = next(
        s for s in deploy["steps"] if s.get("name") == "Images must be pinned by digest"
    )
    assert pin_check["env"]["BACKEND_IMAGE"] == "${{ steps.resolve.outputs.backend_image }}"
    assert pin_check["env"]["FRONTEND_IMAGE"] == "${{ steps.resolve.outputs.frontend_image }}"
    assert pin_check["env"]["OLLAMA_IMAGE"] == "${{ steps.resolve.outputs.ollama_image }}"
    assert deploy["permissions"]["deployments"] == "write"
    assert deploy["permissions"]["id-token"] == "write"
    # The resolve step must run before the apps step (az needs azure/login first too).
    resolve_idx = deploy["steps"].index(resolve)
    apps_idx = next(i for i, s in enumerate(deploy["steps"]) if "inlineScript" in s.get("with", {}))
    assert resolve_idx < apps_idx


def test_ollama_app_is_updated_before_backend_and_frontend(cd: dict) -> None:
    """T11.4.3: the backend calls Ollama synchronously for embeddings, so Ollama must
    already be serving the baked-in model before the backend's new revision can take
    traffic (same "dependency before dependent" order as the migration Job before the
    apps)."""
    deploy = cd["jobs"]["deploy"]
    apps_step = next(s for s in deploy["steps"] if "with" in s and "inlineScript" in s["with"])
    script = apps_step["with"]["inlineScript"]
    ollama_at = script.index("OLLAMA_APP")
    backend_at = script.index("BACKEND_APP")
    frontend_at = script.index("FRONTEND_APP")
    assert ollama_at < backend_at < frontend_at
    # T13.CD.2: the order survives even though each update is now conditional (per-image).
    ollama_update_at = script.index("$OLLAMA_IMAGE")
    backend_update_at = script.index("$BACKEND_IMAGE")
    frontend_update_at = script.index("$FRONTEND_IMAGE")
    assert ollama_update_at < backend_update_at < frontend_update_at
    resolve = next(s for s in deploy["steps"] if s.get("id") == "resolve")
    assert resolve["env"]["OLLAMA_APP"] == "${{ vars.AZURE_OLLAMA_APP }}"
    assert resolve["run"].index('--name "$OLLAMA_APP"') < resolve["run"].index(
        '--name "$BACKEND_APP"'
    )


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


def test_cd_never_touches_ingress_or_the_metrics_port() -> None:
    """DA-11bA-2: `az containerapp update` is only ever called with `--image` here — CD can
    never silently change a Container App's ingress (target port, transport, external/
    internal) or expose the metrics port (9100). Ingress is configured once, by hand, at `az
    containerapp create` time (runbook) and is unaffected by every later CD deploy."""
    commands = _commands(CD_YML)
    for forbidden in ("--ingress", "--target-port", "--transport", "9100", "METRICS_PORT"):
        assert forbidden not in commands, forbidden
    assert commands.count("containerapp update") >= 3  # ollama, backend, frontend


JOB_YAMLS = sorted((REPO_ROOT / "deploy" / "azure" / "jobs").glob("*.yaml"))


DORMANT_CRON = "0 0 1 1 *"  # D-2026-10-01-5: 00:00 UTC on 1 January only


def test_job_definitions_are_dormant_single_placeholder_image_and_secretless() -> None:
    """T11.2.6 / X1: versioned Job YAML applied by hand, placeholder image (CD sets the
    digest), parallelism 1, timeout + retry limit, no secret values and no API secrets
    (JobSettings). The migration Job is Manual; purge/backup are Schedule Jobs with a dormant
    cron (D-2026-10-01-5: az cannot change a trigger type later, only the cron)."""
    assert [p.stem for p in JOB_YAMLS] == ["backup", "migrate", "purge"]
    for path in JOB_YAMLS:
        job = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = job["properties"]["configuration"]
        if path.stem == "migrate":
            assert config["triggerType"] == "Manual"
            trigger = config["manualTriggerConfig"]
        else:
            assert config["triggerType"] == "Schedule", path.name
            assert "manualTriggerConfig" not in config, path.name
            trigger = config["scheduleTriggerConfig"]
            assert trigger["cronExpression"] == DORMANT_CRON, path.name
        assert trigger["parallelism"] == 1 and trigger["replicaCompletionCount"] == 1
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


# --- resolve_image.sh (fake az) ----------------------------------------------------------

FAKE_AZ_RESOLVE = """\
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if "length(" in " ".join(args):
    # DA-13-0-3: resolve_image.sh first asks how many containers there are.
    print(os.environ.get("FAKE_AZ_CONTAINER_COUNT", "1"))
    sys.exit(0)
current = os.environ.get("FAKE_AZ_CURRENT_IMAGE")
if current is None:
    sys.exit(1)  # simulates an unreadable app/Job (az call itself fails)
print(current)
"""

REPO = "ghcr.io/o/r-backend"
BUILT_DIGEST = "sha256:" + "d" * 64


def _fake_az_resolve(
    tmp_path: Path, current_image: str | None, container_count: str | None = None
) -> dict[str, str]:
    bin_dir = tmp_path / "azbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_az.py").write_text(FAKE_AZ_RESOLVE, encoding="utf-8")
    az = bin_dir / "az"
    az.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_az.py"}" "$@"\n')
    az.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
    }
    if current_image is not None:
        env["FAKE_AZ_CURRENT_IMAGE"] = current_image
    if container_count is not None:
        env["FAKE_AZ_CONTAINER_COUNT"] = container_count
    return env


@needs_tools
@pytest.mark.parametrize("kind", ["app", "job"])
def test_resolve_image_build_true_uses_the_built_digest_without_calling_az(
    tmp_path: Path, kind: str
) -> None:
    env = _fake_az_resolve(tmp_path, current_image=None)
    proc = _run(
        RESOLVE_IMAGE,
        kind,
        "--rg",
        "rg",
        "--name",
        "n",
        "--repo",
        REPO,
        "--build",
        "true",
        "--built-digest",
        BUILT_DIGEST,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{REPO}@{BUILT_DIGEST}"
    assert not (tmp_path / "az.log").exists()  # never calls az when build=true


@needs_tools
def test_resolve_image_build_false_reads_the_currently_deployed_app_image(
    tmp_path: Path,
) -> None:
    kept = f"{REPO}@sha256:" + "e" * 64
    env = _fake_az_resolve(tmp_path, current_image=kept)
    proc = _run(
        RESOLVE_IMAGE,
        "app",
        "--rg",
        "rg",
        "--name",
        "myapp",
        "--repo",
        REPO,
        "--build",
        "false",
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == kept
    log = (tmp_path / "az.log").read_text()
    assert "containerapp show" in log and "myapp" in log


@needs_tools
def test_resolve_image_build_false_reads_the_currently_deployed_job_image(
    tmp_path: Path,
) -> None:
    kept = f"{REPO}@sha256:" + "f" * 64
    env = _fake_az_resolve(tmp_path, current_image=kept)
    proc = _run(
        RESOLVE_IMAGE,
        "job",
        "--rg",
        "rg",
        "--name",
        "myjob",
        "--repo",
        REPO,
        "--build",
        "false",
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == kept
    log = (tmp_path / "az.log").read_text()
    assert "containerapp job show" in log and "myjob" in log


@needs_tools
def test_resolve_image_fails_closed_when_az_cannot_read_the_current_image(
    tmp_path: Path,
) -> None:
    env = _fake_az_resolve(tmp_path, current_image=None)
    proc = _run(
        RESOLVE_IMAGE,
        "app",
        "--rg",
        "rg",
        "--name",
        "myapp",
        "--repo",
        REPO,
        "--build",
        "false",
        env=env,
    )
    assert proc.returncode == 1
    assert "could not read the currently deployed image" in proc.stderr
    assert proc.stdout == ""


@needs_tools
@pytest.mark.parametrize(
    "not_pinned", ["ghcr.io/o/r-backend:latest", "ghcr.io/o/r-backend:abc", ""]
)
def test_resolve_image_fails_closed_when_the_kept_image_is_not_digest_pinned(
    tmp_path: Path, not_pinned: str
) -> None:
    env = _fake_az_resolve(tmp_path, current_image=not_pinned)
    proc = _run(
        RESOLVE_IMAGE,
        "app",
        "--rg",
        "rg",
        "--name",
        "myapp",
        "--repo",
        REPO,
        "--build",
        "false",
        env=env,
    )
    assert proc.returncode == 1
    assert "is not pinned by digest" in proc.stderr


@needs_tools
@pytest.mark.parametrize("kind", ["app", "job"])
@pytest.mark.parametrize("count", ["0", "2"])
def test_resolve_image_fails_closed_when_there_is_not_exactly_one_container(
    tmp_path: Path, kind: str, count: str
) -> None:
    """DA-13-0-3: a future sidecar (or a Job/app that never got exactly one container)
    must not silently make `containers[0]` resolve/compare the wrong image."""
    kept = f"{REPO}@sha256:" + "e" * 64
    env = _fake_az_resolve(tmp_path, current_image=kept, container_count=count)
    proc = _run(
        RESOLVE_IMAGE,
        kind,
        "--rg",
        "rg",
        "--name",
        "n",
        "--repo",
        REPO,
        "--build",
        "false",
        env=env,
    )
    assert proc.returncode == 1
    assert f"has {count} containers" in proc.stderr
    assert proc.stdout == ""


# --- DA-13-0-1: the deploy job's purge/backup cross-check (real subprocess, fake az) -----


def _run_cross_check_script(
    cd: dict, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    script_path = tmp_path / "cross_check.sh"
    script_path.write_text(_cross_check_step(cd)["run"], encoding="utf-8")
    # The GitHub-hosted runner's default step shell is `bash --noprofile --norc -eo
    # pipefail {0}` — replicated here since this step (unlike the deploy inlineScript) has
    # no explicit `set -euo pipefail` of its own.
    return subprocess.run(
        ["bash", "-eo", "pipefail", str(script_path)],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _cross_check_env(
    tmp_path: Path, current_image: str | None, migrate_image: str
) -> dict[str, str]:
    env = _fake_az_resolve(tmp_path, current_image=current_image)
    env.update(
        RESOURCE_GROUP="rg",
        PURGE_JOB="purge-job",
        BACKUP_JOB="backup-job",
        REPO="o/r",
        MIGRATE_IMAGE=migrate_image,
    )
    return env


@needs_tools
def test_the_cross_check_step_passes_when_job_images_match_the_migrate_image(
    cd: dict, tmp_path: Path
) -> None:
    kept = "ghcr.io/o/r-jobs@sha256:" + "a" * 64
    env = _cross_check_env(tmp_path, current_image=kept, migrate_image=kept)
    proc = _run_cross_check_script(cd, tmp_path, env)
    assert proc.returncode == 0, proc.stderr


@needs_tools
def test_the_cross_check_step_fails_closed_on_a_mismatch(cd: dict, tmp_path: Path) -> None:
    """DA-13-0-1: an out-of-band `job update` on only one of the three Jobs must be
    caught, naming the Job and both digests — never silently repaired."""
    kept = "ghcr.io/o/r-jobs@sha256:" + "a" * 64
    drifted = "ghcr.io/o/r-jobs@sha256:" + "b" * 64
    env = _cross_check_env(tmp_path, current_image=drifted, migrate_image=kept)
    proc = _run_cross_check_script(cd, tmp_path, env)
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "::error::" in out and "purge-job" in out and kept in out and drifted in out
    assert "no auto-repair" in out


@needs_tools
def test_the_cross_check_step_fails_closed_when_a_job_image_is_unreadable(
    cd: dict, tmp_path: Path
) -> None:
    env = _cross_check_env(
        tmp_path, current_image=None, migrate_image="ghcr.io/o/r-jobs@sha256:" + "a" * 64
    )
    proc = _run_cross_check_script(cd, tmp_path, env)
    assert proc.returncode != 0


@needs_tools
def test_resolve_image_build_true_without_a_built_digest_is_refused(tmp_path: Path) -> None:
    env = _fake_az_resolve(tmp_path, current_image=None)
    proc = _run(
        RESOLVE_IMAGE,
        "app",
        "--rg",
        "rg",
        "--name",
        "myapp",
        "--repo",
        REPO,
        "--build",
        "true",
        env=env,
    )
    assert proc.returncode == 1
    assert "needs --built-digest" in proc.stderr
    assert not (tmp_path / "az.log").exists()


@needs_tools
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus", "--rg", "rg", "--name", "n", "--repo", REPO],
        ["app", "--name", "n", "--repo", REPO],  # missing --rg
        ["app", "--rg", "rg", "--repo", REPO],  # missing --name
        ["app", "--rg", "rg", "--name", "n"],  # missing --repo
        ["app", "--rg", "rg", "--name", "n", "--repo", REPO, "--build", "maybe"],
    ],
)
def test_resolve_image_usage_errors(tmp_path: Path, args: list[str]) -> None:
    env = _fake_az_resolve(tmp_path, current_image=None)
    proc = _run(RESOLVE_IMAGE, *args, env=env)
    assert proc.returncode == 2


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
    """With --wait the check polls: a background publisher may post a little later.

    Deterministic (11a open item / 11b flaky-test fix): GATE_STATUS_FAKE_CLOCK replaces
    real wall-clock sleeping with a fake counter inside gate_status.sh that advances by
    exactly one second per poll (same --wait loop, same deadline arithmetic — only the
    time source differs), so the poll count no longer depends on real scheduling. Before
    this fix the test slept for real (GATE_STATUS_POLL_SECONDS=0.3, --wait 1) and failed
    once on CI under load (fewer than 2 polls completed in the 1 s budget).
    """
    env = _fake_gh(tmp_path, [{"match": "statuses", "stdout": " \n"}])
    proc = _run(
        GATE_STATUS,
        "verify",
        SHA,
        "--wait",
        "2",
        env={**env, "GATE_STATUS_FAKE_CLOCK": "1"},
    )
    assert proc.returncode == 1
    polls = (tmp_path / "gh.log").read_text(encoding="utf-8").count("statuses")
    # Exact and deterministic: fake clock 0,1,2 -> 3 polls before "2 < 2" breaks the loop.
    assert polls == 3


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


# --- plan.sh per-image outputs (T13.CD.1) ------------------------------------------------


@needs_tools
def test_backend_change_flags_backend_and_jobs_only(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], "--base", shas["docs"])
    assert out["deploy"] == "true", proc.stdout
    assert out["build_backend"] == "true" and out["build_jobs"] == "true"
    assert out["build_frontend"] == "false" and out["build_ollama"] == "false"


@needs_tools
def test_docs_only_change_flags_no_image(tmp_path: Path, history) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["docs"], "--base", shas["base"])
    assert out["deploy"] == "false", proc.stdout
    for image in ("backend", "frontend", "jobs", "ollama"):
        assert out[f"build_{image}"] == "false", image


@needs_tools
@pytest.mark.parametrize(
    ("path", "expected_true", "expected_false"),
    [
        ("frontend/app/page.tsx", ["frontend"], ["backend", "jobs", "ollama"]),
        ("ollama/Dockerfile", ["ollama"], ["backend", "frontend", "jobs"]),
        (
            ".github/workflows/cd.yml",
            ["backend", "frontend", "jobs", "ollama"],
            [],
        ),
        (
            "scripts/cd/plan.sh",
            ["backend", "frontend", "jobs", "ollama"],
            [],
        ),
        # Unmapped, non-skippable path: fail-safe widens to all four (header comment point 5).
        (
            "db/roles.sql",
            ["backend", "frontend", "jobs", "ollama"],
            [],
        ),
        (
            "scripts/db/backup.sh",
            ["backend", "frontend", "jobs", "ollama"],
            [],
        ),
    ],
    ids=[
        "frontend-only",
        "ollama-only",
        "cd-yml",
        "plan-sh-self",
        "unmapped-db-roles",
        "unmapped-scripts-db",
    ],
)
def test_per_image_mapping_single_path(
    tmp_path: Path,
    base_repo: tuple[Path, str],
    path: str,
    expected_true: list[str],
    expected_false: list[str],
) -> None:
    repo, base = base_repo
    sha = _commit(repo, {path: "x"}, f"touch {path}")
    proc, out = _plan(tmp_path, repo, "--sha", sha, "--base", base)
    assert out["deploy"] == "true", proc.stdout
    for image in expected_true:
        assert out[f"build_{image}"] == "true", (path, image, proc.stdout)
    for image in expected_false:
        assert out[f"build_{image}"] == "false", (path, image, proc.stdout)


@needs_tools
def test_a_backend_and_frontend_change_together_flags_three_images(
    tmp_path: Path, base_repo: tuple[Path, str]
) -> None:
    repo, base = base_repo
    sha = _commit(repo, {"backend/app.py": "2", "frontend/app/page.tsx": "2"}, "both")
    proc, out = _plan(tmp_path, repo, "--sha", sha, "--base", base)
    assert out["deploy"] == "true", proc.stdout
    assert out["build_backend"] == "true"
    assert out["build_jobs"] == "true"
    assert out["build_frontend"] == "true"
    assert out["build_ollama"] == "false"


@needs_tools
def test_force_all_builds_everything_even_for_a_backend_only_change(
    tmp_path: Path, history
) -> None:
    repo, shas = history
    proc, out = _plan(tmp_path, repo, "--sha", shas["code"], "--base", shas["docs"], "--force-all")
    assert out["deploy"] == "true", proc.stdout
    for image in ("backend", "frontend", "jobs", "ollama"):
        assert out[f"build_{image}"] == "true", image
    assert "--force-all" in proc.stdout


@needs_tools
def test_force_all_never_turns_a_skipped_sha_into_a_deploy(tmp_path: Path, history) -> None:
    """force_all only widens the per-image mapping once build/deploy is already going ahead
    — it must never override the no-rollback / docs-only safety skips."""
    repo, shas = history
    # docs-only: still skipped even with --force-all.
    proc, out = _plan(tmp_path, repo, "--sha", shas["docs"], "--base", shas["base"], "--force-all")
    assert out["deploy"] == "false", proc.stdout
    for image in ("backend", "frontend", "jobs", "ollama"):
        assert out[f"build_{image}"] == "false", image
    # ancestor of the deployed SHA: never deployed, even with --force-all.
    proc, out = _plan(tmp_path, repo, "--sha", shas["base"], "--base", shas["code"], "--force-all")
    assert out["deploy"] == "false", proc.stdout
    assert "never rolls back" in out["reason"]


# --- plan.sh skippable() (DEBT-023, T11c.E5 option B) ------------------------------------


@pytest.fixture
def base_repo(tmp_path: Path) -> tuple[Path, str]:
    """A fresh repo with just one commit, so a second commit's diff is exactly one path."""
    repo = tmp_path / "base_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    base = _commit(repo, {"backend/app.py": "1", "README.md": "a"}, "base")
    return repo, base


NEW_SKIPPABLE_PATHS = [
    "deploy/prometheus/prometheus.yml",
    "eval/golden_set.jsonl",
    "data/chunks/x.jsonl",
    "backend/tests/test_x.py",
    "backend/requirements-dev.txt",
    "backend/pyproject.toml",
    "scripts/azure/demo-mode.sh",
    "scripts/dev/create_dev_volume.sh",
    "scripts/prereqs/install.sh",
    "scripts/gate.sh",
    "scripts/fetch_corpus.py",
    "scripts/restart_check.sh",
    "scripts/restart_check.compose.yml",
    "frontend/lib/chatStream.test.ts",
]

# Never widened into the skip list, even though most changes there are harmless too (see the
# plan.sh header comment for why each one stays a deploy trigger).
EXCLUDED_RISKY_PATHS = [
    "scripts/db/backup.sh",  # baked into the jobs image via the dbscripts build context
    "scripts/db/apply_roles.sh",  # same directory as backup.sh — never widened as a whole
    "scripts/cd/plan.sh",  # this script's own logic — self-referential risk
    "db/roles.sql",  # not read by CD, but a human must still re-apply it by hand
    "deploy/azure/jobs/migrate.yaml",  # same: never read by CD, a human acts on it
]


@needs_tools
@pytest.mark.parametrize("path", NEW_SKIPPABLE_PATHS)
def test_each_new_skippable_path_alone_skips_build_and_deploy(
    tmp_path: Path, base_repo: tuple[Path, str], path: str
) -> None:
    repo, base = base_repo
    sha = _commit(repo, {path: "x"}, f"touch {path}")
    proc, out = _plan(tmp_path, repo, "--sha", sha, "--base", base)
    assert proc.returncode == 0, proc.stderr
    assert out["deploy"] == "false", proc.stdout


@needs_tools
def test_a_skippable_path_mixed_with_a_real_image_input_still_deploys(
    tmp_path: Path, base_repo: tuple[Path, str]
) -> None:
    repo, base = base_repo
    sha = _commit(
        repo,
        {"backend/tests/test_x.py": "x", "backend/src/rag_app/config.py": "2"},
        "mixed change",
    )
    proc, out = _plan(tmp_path, repo, "--sha", sha, "--base", base)
    assert out["deploy"] == "true", proc.stdout
    assert "backend/src/rag_app/config.py" in out["reason"]


@needs_tools
@pytest.mark.parametrize("path", EXCLUDED_RISKY_PATHS)
def test_each_excluded_risky_path_alone_still_deploys(
    tmp_path: Path, base_repo: tuple[Path, str], path: str
) -> None:
    repo, base = base_repo
    sha = _commit(repo, {path: "x"}, f"touch {path}")
    proc, out = _plan(tmp_path, repo, "--sha", sha, "--base", base)
    assert out["deploy"] == "true", proc.stdout
    assert path in out["reason"]


def _skippable_patterns() -> list[str]:
    """The literal case patterns of plan.sh's skippable(), parsed from its own source so this
    guard test can never drift from what the script actually runs."""
    text = PLAN.read_text(encoding="utf-8")
    body = re.search(r"skippable\(\)\s*\{.*?case \"\$1\" in(.*?)\)\s*return 0", text, re.S)
    assert body, "could not find skippable()'s case patterns in plan.sh"
    raw = body.group(1).replace("\\\n", " ")
    return [p.strip() for p in raw.split("|") if p.strip()]


def _dockerfile_copy_sources(dockerfile: Path, context: Path) -> set[str]:
    """Host-relative paths a Dockerfile's plain ``COPY`` (never ``--from=<stage>``, which
    copies from a previous build STAGE, not the host) instructions actually copy, resolved
    against its build ``context`` (as in cd.yml). ``COPY --from=dbscripts`` is the one named
    (non-stage) build context this repo uses (``scripts/db``, see Dockerfile.jobs/cd.yml) and
    is resolved against it explicitly."""
    sources: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*COPY\s+(.*)", line)
        if not match:
            continue
        parts = match.group(1).split()
        base = context
        if parts and parts[0].startswith("--from="):
            stage = parts[0].removeprefix("--from=")
            if stage != "dbscripts":
                continue  # from a previous build STAGE, not the host — nothing to resolve
            base = REPO_ROOT / "scripts" / "db"
            parts = parts[1:]
        parts = [p for p in parts if not p.startswith("--")]
        for src in parts[:-1]:  # the last argument is always the destination
            rel = os.path.relpath(base / src, REPO_ROOT)
            sources.add(rel.replace(os.sep, "/"))
    return sources


def test_the_skip_list_never_matches_a_real_dockerfile_copy_source() -> None:
    """DEBT-023, T11c.E5 option B: if a future Dockerfile change starts COPYing a path this
    skip list treats as harmless, THIS test fails — not CD silently skipping a real change."""
    import fnmatch

    patterns = _skippable_patterns()
    sources: set[str] = set()
    sources |= _dockerfile_copy_sources(REPO_ROOT / "backend" / "Dockerfile", REPO_ROOT / "backend")
    sources |= _dockerfile_copy_sources(
        REPO_ROOT / "backend" / "Dockerfile.jobs", REPO_ROOT / "backend"
    )
    sources |= _dockerfile_copy_sources(REPO_ROOT / "ollama" / "Dockerfile", REPO_ROOT / "ollama")
    # frontend/Dockerfile's builder stage COPYs the whole context ("COPY . .") on purpose
    # (T11c.E5 analysis: *.test.ts never reaches the FINAL image); checked separately below.
    assert sources, "expected at least one real COPY source to check against"
    for source in sources:
        for pattern in patterns:
            assert not fnmatch.fnmatch(source, pattern), (
                f"{source} is COPYed into an image but matches skip pattern {pattern!r} "
                "— widen it out of skippable() in plan.sh"
            )


def test_frontend_final_image_never_copies_raw_source_only_the_next_build_output() -> None:
    """Backs the T11c.E5 analysis that frontend/**/*.test.ts (copied into the builder stage,
    never bundled) is safe to skip: the FINAL stage must only take .next/standalone and
    .next/static from the builder, never the raw context."""
    text = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    runner = text.split("AS runner", 1)[1]
    copies = re.findall(r"COPY --from=builder (\S+)", runner)
    assert copies, "expected the runner stage to COPY --from=builder"
    assert all(src.startswith("/app/.next/") for src in copies), copies


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


# --- Post-deploy health check (scripts/cd/health_check.sh; deferred from 11a) ------------


class _FixedStatusHandler(BaseHTTPRequestHandler):
    status_code = 200

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self.send_response(self.status_code)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # silence per-request stderr noise
        pass


@pytest.fixture
def http_server() -> Iterator[tuple[str, type[_FixedStatusHandler]]]:
    """A real, throwaway HTTP server on 127.0.0.1 answering every request with
    ``handler.status_code`` (mutable per test) — no fake process, no mocked curl."""
    handler = type(f"Handler{id(object())}", (_FixedStatusHandler,), {"status_code": 200})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


@needs_tools
def test_health_check_passes_once_the_expected_status_is_served(http_server) -> None:
    base_url, handler = http_server
    handler.status_code = 200
    proc = _run(
        HEALTH_CHECK, f"{base_url}/health", "--timeout", "5", "--interval", "1", env=os.environ
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{base_url}/health -> 200 (ok)" in proc.stdout


@needs_tools
def test_health_check_accepts_a_non_200_expected_status(http_server) -> None:
    base_url, handler = http_server
    handler.status_code = 404
    proc = _run(
        HEALTH_CHECK,
        f"{base_url}/x",
        "--timeout",
        "5",
        "--interval",
        "1",
        "--expect-status",
        "404",
        env=os.environ,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_tools
def test_health_check_times_out_on_the_wrong_status(http_server) -> None:
    base_url, handler = http_server
    handler.status_code = 500
    proc = _run(
        HEALTH_CHECK, f"{base_url}/health", "--timeout", "2", "--interval", "1", env=os.environ
    )
    assert proc.returncode == 1
    assert "did not answer 200 within 2s" in proc.stderr
    assert "last status: 500" in proc.stderr


@needs_tools
def test_health_check_times_out_when_nothing_is_listening(tmp_path: Path) -> None:
    # An unused local port: a real connection-level failure, not an HTTP error.
    proc = _run(
        HEALTH_CHECK,
        "http://127.0.0.1:1/health",
        "--timeout",
        "2",
        "--interval",
        "1",
        env=os.environ,
    )
    assert proc.returncode == 1
    assert "last status: 000" in proc.stderr


@needs_tools
def test_health_check_eventually_succeeds_after_the_app_wakes_up(http_server) -> None:
    """The realistic case: a scale-to-zero app is still starting when the first poll runs."""
    base_url, handler = http_server
    handler.status_code = 503

    def _wake_up() -> None:
        import time

        time.sleep(1.2)
        handler.status_code = 200

    threading.Thread(target=_wake_up, daemon=True).start()
    proc = _run(
        HEALTH_CHECK, f"{base_url}/health", "--timeout", "5", "--interval", "1", env=os.environ
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_tools
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["http://x", "--timeout", "nope"],
        ["http://x", "--expect-status", "abc"],
        ["http://x", "--bogus"],
    ],
)
def test_health_check_usage_errors(args: list[str]) -> None:
    assert _run(HEALTH_CHECK, *args, env=os.environ).returncode == 2


def test_health_check_never_prints_a_response_body() -> None:
    """No secrets rule: the script only ever prints the URL and the HTTP status."""
    text = HEALTH_CHECK.read_text(encoding="utf-8")
    assert "-o /dev/null" in text
    assert "-w '%{http_code}'" in text


def test_deploy_job_polls_health_after_updating_the_apps_and_before_the_job_images(
    cd: dict,
) -> None:
    deploy = cd["jobs"]["deploy"]
    steps = deploy["steps"]
    apps_idx = next(i for i, s in enumerate(steps) if "inlineScript" in s.get("with", {}))
    jobs_idx = next(
        i for i, s in enumerate(steps) if "azure_jobs.sh update-image" in s.get("run", "")
    )
    health_runs = [i for i, s in enumerate(steps) if "health_check.sh" in s.get("run", "")]
    assert len(health_runs) >= 2, "backend AND frontend must both be polled"
    assert apps_idx < min(health_runs) < jobs_idx
    text = "\n".join(s.get("run", "") for s in steps if "health_check.sh" in s.get("run", ""))
    assert "/health" in text  # backend readiness endpoint
    # A failed health check must fail the job (no `continue-on-error`, no `|| true`).
    assert not any(
        s.get("continue-on-error") for s in steps if "health_check.sh" in s.get("run", "")
    )


def test_dry_run_echoes_the_health_check_step(cd: dict) -> None:
    names = [s.get("name", "") for s in cd["jobs"]["dry-run"]["steps"] if "name" in s]
    assert [n.split(".")[0] for n in names] == ["0", "1", "2", "3", "4", "5"]
    assert "health" in names[4].lower()


def test_backend_health_check_goes_through_the_frontends_bff_not_its_own_fqdn(
    cd: dict,
) -> None:
    """DEC-005/T12.5.6 (2026-11-12): once the backend's ingress goes internal (T12.6b.5) it
    has no public FQDN to poll directly any more -- the backend half of the post-deploy
    health check must go through the frontend's own `/api/health` BFF route instead, and
    no step in this job resolves a backend ingress FQDN/URL at all any more."""
    deploy = cd["jobs"]["deploy"]
    steps = deploy["steps"]
    health_runs = [s.get("run", "") for s in steps if "health_check.sh" in s.get("run", "")]
    assert len(health_runs) == 2, "backend AND frontend must both be polled"
    assert any("FRONTEND_URL" in r and "/api/health" in r for r in health_runs)
    assert not any("BACKEND_URL" in r for r in health_runs)
    fqdn_steps_text = "\n".join(
        s.get("run", "") for s in steps if "ingress.fqdn" in s.get("run", "")
    )
    assert "BACKEND_APP" not in fqdn_steps_text
    assert "FRONTEND_APP" in fqdn_steps_text


def test_dry_run_health_echo_mentions_the_bff_path(cd: dict) -> None:
    steps = cd["jobs"]["dry-run"]["steps"]
    health_step = next(s for s in steps if "health" in s.get("name", "").lower())
    assert "/api/health" in health_step["run"]


# --- DA-11bD-2: the 3-app deploy inlineScript must not partially deploy on a failure -----


def _deploy_inline_script(cd: dict) -> str:
    deploy = cd["jobs"]["deploy"]
    return next(
        s["with"]["inlineScript"]
        for s in deploy["steps"]
        if "with" in s and "inlineScript" in s["with"]
    )


# DA-13-0-2: the inlineScript no longer has any `${{ }}` to render — every value reaches it
# through `env:` (the same way azure/cli@v3 forwards a step's env into its container), so
# these tests just set the env vars directly, the way the Actions runner would.
_INLINE_ENV_DEFAULTS = {
    "OLLAMA_APP": "ollama-app",
    "BACKEND_APP": "backend-app",
    "FRONTEND_APP": "frontend-app",
    "RESOURCE_GROUP": "rg",
    # T13.CD.2: the inlineScript gates each app update on its own build flag and consumes
    # the digest resolved by the earlier "Resolve app image digests" step.
    "BUILD_OLLAMA": "true",
    "BUILD_BACKEND": "true",
    "BUILD_FRONTEND": "true",
    "OLLAMA_IMAGE": "ghcr.io/o/r-ollama@sha256:" + "a" * 64,
    "BACKEND_IMAGE": "ghcr.io/o/r-backend@sha256:" + "b" * 64,
    "FRONTEND_IMAGE": "ghcr.io/o/r-frontend@sha256:" + "c" * 64,
}


FAKE_AZ_CONTAINERAPP_UPDATE = """\
import sys, os
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
sys.exit(0)
"""


def _fake_az_containerapp_update(tmp_path: Path, **overrides: str) -> dict[str, str]:
    bin_dir = tmp_path / "azbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_az.py").write_text(FAKE_AZ_CONTAINERAPP_UPDATE, encoding="utf-8")
    az = bin_dir / "az"
    az.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_az.py"}" "$@"\n')
    az.chmod(0o755)
    return {
        **os.environ,
        **_INLINE_ENV_DEFAULTS,
        **overrides,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
    }


def _az_update_log(tmp_path: Path) -> list[str]:
    log = tmp_path / "az.log"
    return log.read_text().splitlines() if log.exists() else []


def test_deploy_inline_script_starts_with_an_explicit_pipefail(cd: dict) -> None:
    """DA-11bD-2: no longer relying on azure/cli@v2's own (undocumented) default — a
    failed `az containerapp update` must stop this script before the next app is
    touched, now that it deploys three apps (ollama, backend, frontend) in sequence."""
    script = _deploy_inline_script(cd)
    assert script.strip().splitlines()[0] == "set -euo pipefail"


@needs_tools
def test_deploy_inline_script_fails_before_touching_any_app_when_ollama_app_is_missing(
    cd: dict, tmp_path: Path
) -> None:
    """A missing/empty AZURE_OLLAMA_APP repo var (e.g. not yet created before the first
    real CD run deploys three apps through this step) must fail BEFORE any
    `az containerapp update` call — never silently skip ollama and still update
    backend/frontend (a worse partial-deploy state than touching nothing)."""
    script = _deploy_inline_script(cd)
    script_path = tmp_path / "deploy.sh"
    script_path.write_text(script, encoding="utf-8")
    env = _fake_az_containerapp_update(tmp_path, OLLAMA_APP="")
    proc = subprocess.run(
        ["bash", str(script_path)], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    assert "AZURE_OLLAMA_APP" in (proc.stdout + proc.stderr)
    assert _az_update_log(tmp_path) == []  # not even ollama's own update ran


@needs_tools
def test_deploy_inline_script_updates_all_three_apps_in_order_when_vars_are_set(
    cd: dict, tmp_path: Path
) -> None:
    script = _deploy_inline_script(cd)
    script_path = tmp_path / "deploy.sh"
    script_path.write_text(script, encoding="utf-8")
    env = _fake_az_containerapp_update(tmp_path)
    proc = subprocess.run(
        ["bash", str(script_path)], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    log = _az_update_log(tmp_path)
    assert len(log) == 3
    assert "--name ollama-app" in log[0] and "sha256:" + "a" * 64 in log[0]
    assert "--name backend-app" in log[1] and "sha256:" + "b" * 64 in log[1]
    assert "--name frontend-app" in log[2] and "sha256:" + "c" * 64 in log[2]


@needs_tools
def test_deploy_inline_script_leaves_an_unflagged_app_untouched(cd: dict, tmp_path: Path) -> None:
    """T13.CD.2: an app the plan did not flag (e.g. only frontend changed) is never passed
    to `az containerapp update` — it keeps running the image resolved earlier (unchanged)."""
    script = _deploy_inline_script(cd)
    script_path = tmp_path / "deploy.sh"
    script_path.write_text(script, encoding="utf-8")
    env = _fake_az_containerapp_update(tmp_path, BUILD_OLLAMA="false", BUILD_BACKEND="false")
    proc = subprocess.run(
        ["bash", str(script_path)], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    log = _az_update_log(tmp_path)
    assert len(log) == 1  # only frontend (still flagged true by the defaults) was updated
    assert "--name frontend-app" in log[0] and "sha256:" + "c" * 64 in log[0]
    assert "ollama-app" not in " ".join(log) and "backend-app" not in " ".join(log)


@needs_tools
def test_deploy_inline_script_stops_after_the_first_app_if_az_fails(
    cd: dict, tmp_path: Path
) -> None:
    """Proves `set -euo pipefail` actually stops the sequence: with a fake `az` that
    fails every call, only the FIRST `containerapp update` (ollama) must run — backend
    and frontend must never be touched after ollama's own update failed."""
    script = _deploy_inline_script(cd)
    script_path = tmp_path / "deploy.sh"
    script_path.write_text(script, encoding="utf-8")
    bin_dir = tmp_path / "azbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_az.py").write_text(
        "import sys, os\n"
        "args = sys.argv[1:]\n"
        'with open(os.environ["FAKE_AZ_LOG"], "a") as log:\n'
        '    log.write(" ".join(args) + "\\n")\n'
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    az = bin_dir / "az"
    az.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_az.py"}" "$@"\n')
    az.chmod(0o755)
    env = {
        **os.environ,
        **_INLINE_ENV_DEFAULTS,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
    }
    proc = subprocess.run(
        ["bash", str(script_path)], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    log = _az_update_log(tmp_path)
    assert len(log) == 1  # only ollama's own update ran before the script stopped
    assert "--name ollama-app" in log[0]
