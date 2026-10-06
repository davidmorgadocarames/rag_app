"""Demo guard (T11.6b.8, block H, 11b): `scripts/azure/demo-guard.sh` and
`.github/workflows/demo-guard.yml`. `az` is replaced by a fake (same pattern as
test_demo_mode.py), so no test talks to Azure; the workflow's structure is checked
statically (yaml.safe_load), same pattern as test_cd_pipeline.py's cd.yml invariants.
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
DEMO_GUARD = REPO_ROOT / "scripts" / "azure" / "demo-guard.sh"
DEMO_GUARD_YML = REPO_ROOT / ".github" / "workflows" / "demo-guard.yml"

needs_tools = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

APPS = {"backend": "b", "ollama": "o", "frontend": "f"}
TAG_KEY = "secrag-demo-until"

# Mirrors test_demo_mode.py's fake az, plus a hard guard: this fake refuses ANY attempt to
# set min-replicas to anything other than 0 (demo-guard.sh must never scale up).
FAKE_AZ = r"""
import json, os, sys
state_path = os.environ["FAKE_AZ_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}
args = sys.argv[1:]
with open(os.environ["FAKE_AZ_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")

def val(flag):
    return args[args.index(flag) + 1]

def app_state(app):
    return state.setdefault(app, {"min": "0", "tag": ""})

if args[:2] == ["containerapp", "update"]:
    app = val("-n")
    new_min = val("--min-replicas")
    if new_min != "0":
        sys.exit(f"REFUSED: demo-guard tried to set min-replicas={new_min} (must be 0)\n")
    assert val("--max-replicas") == "1"
    app_state(app)["min"] = new_min
elif args[:2] == ["containerapp", "show"]:
    app = val("-n")
    query = val("--query")
    st = app_state(app)
    if query == "properties.template.scale.minReplicas":
        print(st["min"])
    elif query.startswith("tags."):
        print(st.get("tag", ""))
    elif query == "id":
        print(f"id-{app}")
    else:
        sys.exit(3)
elif args[:2] == ["tag", "update"]:
    resource_id = val("--resource-id")
    app = resource_id.removeprefix("id-")
    op = val("--operation")
    st = app_state(app)
    if op == "delete":
        st["tag"] = ""
    else:
        sys.exit(3)
else:
    sys.exit(3)
json.dump(state, open(state_path, "w"))
"""


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
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
        "AZURE_RESOURCE_GROUP": "rg",
        "AZURE_BACKEND_APP": APPS["backend"],
        "AZURE_OLLAMA_APP": APPS["ollama"],
        "AZURE_FRONTEND_APP": APPS["frontend"],
        **extra,
    }


def _seed(tmp_path: Path, state: dict) -> None:
    (tmp_path / "az.json").write_text(json.dumps(state))


def _state(tmp_path: Path) -> dict:
    path = tmp_path / "az.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _run(*args: str, env: dict[str, str]):
    return subprocess.run(
        ["bash", str(DEMO_GUARD), *args], env=env, capture_output=True, text=True, timeout=60
    )


# --- demo-guard.sh ------------------------------------------------------------------------


@needs_tools
def test_scales_down_an_app_with_an_expired_tag(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _seed(
        tmp_path,
        {app: {"min": "1", "tag": "2000-01-01T00:00:00Z"} for app in APPS.values()},
    )
    proc = _run(env=env)
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "0"
        assert state[app]["tag"] == ""
    assert "scaling DOWN" in proc.stdout and "scaled down" in proc.stdout


@needs_tools
def test_scales_down_an_app_with_no_tag_at_all(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _seed(tmp_path, {app: {"min": "1", "tag": ""} for app in APPS.values()})
    proc = _run(env=env)
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "0"


@needs_tools
def test_leaves_an_app_with_a_future_tag_running(tmp_path: Path) -> None:
    env = _env(tmp_path)
    future = "2099-01-01T00:00:00Z"
    _seed(tmp_path, {app: {"min": "1", "tag": future} for app in APPS.values()})
    proc = _run(env=env)
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "1"
        assert state[app]["tag"] == future
    assert "leaving it running" in proc.stdout


@needs_tools
def test_leaves_an_already_resting_app_alone(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _seed(tmp_path, {app: {"min": "0", "tag": ""} for app in APPS.values()})
    proc = _run(env=env)
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "az.log").read_text() if (tmp_path / "az.log").exists() else ""
    assert "containerapp update" not in log, "an app already at rest must not be touched"


@needs_tools
def test_the_script_can_never_scale_up(tmp_path: Path) -> None:
    """The fake az itself refuses any min-replicas != 0; demo-guard.sh never asks for one."""
    env = _env(tmp_path)
    _seed(tmp_path, {app: {"min": "1", "tag": "2000-01-01T00:00:00Z"} for app in APPS.values()})
    proc = _run(env=env)
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED" not in proc.stderr and "REFUSED" not in proc.stdout
    for line in (tmp_path / "az.log").read_text().splitlines():
        if line.startswith("containerapp update"):
            assert "--min-replicas 0" in line
            assert "--max-replicas 1" in line


@needs_tools
def test_dry_run_never_calls_az(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = _run("--dry-run", env=env)
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "az.json").exists()
    assert not (tmp_path / "az.log").exists()


@needs_tools
def test_missing_required_flags_are_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["AZURE_RESOURCE_GROUP"]
    proc = _run(env=env)
    assert proc.returncode != 0
    assert "--rg is required" in proc.stderr


# --- demo-guard.yml ------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(DEMO_GUARD_YML.read_text(encoding="utf-8"))


def test_workflow_runs_hourly_and_supports_manual_dry_run() -> None:
    workflow = _workflow()
    on = workflow[True] if True in workflow else workflow["on"]
    assert "schedule" in on, "demo-guard.yml must have an hourly cron trigger"
    crons = [entry["cron"] for entry in on["schedule"]]
    assert any(cron.split()[1] == "*" for cron in crons), "the cron must fire every hour"
    assert "workflow_dispatch" in on
    dry_run_input = on["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run_input["default"] is True
    assert dry_run_input["type"] == "boolean"


def test_workflow_calls_demo_guard_sh_and_skips_azure_login_on_a_dry_run() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["guard"]
    text = yaml.dump(job)
    assert "demo-guard.sh" in text
    assert "--dry-run" in text
    login_step = next(s for s in job["steps"] if "azure/login" in str(s.get("uses", "")))
    assert login_step.get("if") == "steps.mode.outputs.dry_run == 'false'", (
        "a dry run must skip the Azure login step entirely (no OIDC token requested)"
    )


def test_workflow_never_uses_the_environment_key() -> None:
    """Keeps the OIDC subject at repo:<owner>/<repo>:ref:refs/heads/main (runbook gotcha 8) --
    an `environment:` key would change it to environment:<name> instead."""
    text = DEMO_GUARD_YML.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    for job in workflow["jobs"].values():
        assert "environment" not in job


def test_workflow_never_grants_write_permissions_beyond_id_token() -> None:
    """Defence in depth: this workflow only ever needs to read the repo and mint an OIDC
    token -- never contents:write, never anything that could push code or modify the repo."""
    workflow = _workflow()
    for job in workflow["jobs"].values():
        for scope, level in job.get("permissions", {}).items():
            assert level == "read" or scope == "id-token", (
                f"unexpected write permission: {scope}={level}"
            )
