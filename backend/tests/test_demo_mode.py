"""Demo mode script (T11.6b.7, block H, 11b): `scripts/azure/demo-mode.sh on|off|status`.

`az` and `curl` are replaced by fakes (same pattern as test_cd_pipeline.py's `azure_jobs.sh`
tests), so no test talks to Azure. The fake `az` keeps a small per-app JSON state (min
replicas + the `secrag-demo-until` tag) so `on`/`off`/`status` can be exercised end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_MODE = REPO_ROOT / "scripts" / "azure" / "demo-mode.sh"

needs_tools = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

APPS = {"backend": "b", "ollama": "o", "frontend": "f"}
TAG_KEY = "secrag-demo-until"

# A minimal fake `az` that understands exactly the commands demo-mode.sh issues:
#   containerapp update   -g RG -n APP --min-replicas N --max-replicas 1
#   containerapp show     -g RG -n APP --query <field> -o tsv
#   tag update            --resource-id id-APP --operation merge|delete --tags KEY[=VALUE]
# State is a JSON file {app: {"min": 0|1, "tag": "...", "running": "Running"}}.
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
    return state.setdefault(app, {"min": "0", "tag": "", "running": "Running"})

if args[:2] == ["containerapp", "update"]:
    app = val("-n")
    app_state(app)["min"] = val("--min-replicas")
    assert val("--max-replicas") == "1", "max-replicas must always be 1"
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
    elif query == "properties.configuration.ingress.fqdn":
        print(f"{app}.example.com")
    elif query == "properties.runningStatus":
        seq = os.environ.get("FAKE_AZ_RUNNING_SEQ")
        if seq:
            polls = state.setdefault("_running_polls", {})
            n = polls.get(app, 0)
            steps = seq.split(",")
            print(steps[min(n, len(steps) - 1)])
            polls[app] = n + 1
        else:
            print(st.get("running", "Running"))
    else:
        sys.exit(3)
elif args[:2] == ["tag", "update"]:
    resource_id = val("--resource-id")
    app = resource_id.removeprefix("id-")
    op = val("--operation")
    tags = val("--tags")
    st = app_state(app)
    if op == "merge":
        key, _, value = tags.partition("=")
        assert key == "secrag-demo-until"
        st["tag"] = value
    elif op == "delete":
        st["tag"] = ""
    else:
        sys.exit(3)
else:
    sys.exit(3)
json.dump(state, open(state_path, "w"))
"""

FAKE_CURL = "#!/bin/sh\nprintf '200'\n"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "fake_az.py").write_text(FAKE_AZ, encoding="utf-8")
    az = bin_dir / "az"
    az.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_az.py"}" "$@"\n')
    az.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AZ_STATE": str(tmp_path / "az.json"),
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
        "AZURE_DEMO_POLL_SECONDS": "0",
        "AZURE_DEMO_RUNNING_TIMEOUT_SECONDS": "5",
        "AZURE_DEMO_HEALTH_TIMEOUT_SECONDS": "5",
        "AZURE_RESOURCE_GROUP": "rg",
        "AZURE_BACKEND_APP": APPS["backend"],
        "AZURE_OLLAMA_APP": APPS["ollama"],
        "AZURE_FRONTEND_APP": APPS["frontend"],
        **extra,
    }


def _run(*args: str, env: dict[str, str]):
    return subprocess.run(
        ["bash", str(DEMO_MODE), *args], env=env, capture_output=True, text=True, timeout=60
    )


def _state(tmp_path: Path) -> dict:
    path = tmp_path / "az.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _log(tmp_path: Path) -> list[str]:
    path = tmp_path / "az.log"
    return path.read_text().splitlines() if path.exists() else []


@needs_tools
def test_on_wakes_all_three_apps_tags_them_and_checks_health(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = _run("on", env=env)
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "1"
        assert state[app]["tag"], f"{app} must be tagged"
    assert "ready" in proc.stdout


@needs_tools
def test_on_never_restarts_an_already_active_app_only_extends_the_tag(tmp_path: Path) -> None:
    env = _env(tmp_path)
    first = _run("on", env=env)
    assert first.returncode == 0, first.stderr
    first_tag = {app: _state(tmp_path)[app]["tag"] for app in APPS.values()}

    # advance the fake clock artificially: capture the update-call count, re-run `on`.
    updates_before = sum(1 for line in _log(tmp_path) if line.startswith("containerapp update"))
    second = _run("on", env=env)
    assert second.returncode == 0, second.stderr
    updates_after = sum(1 for line in _log(tmp_path) if line.startswith("containerapp update"))

    assert (
        updates_after == updates_before
    ), "an already min-replicas=1 app must not be updated again"
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["tag"] >= first_tag[app]


@needs_tools
def test_max_replicas_is_always_exactly_one(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = _run("on", env=env)
    assert proc.returncode == 0, proc.stderr
    for line in _log(tmp_path):
        if line.startswith("containerapp update"):
            assert "--max-replicas 1" in line


@needs_tools
def test_demo_mode_never_touches_env_vars_or_secrets(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for action in ("on", "off", "status"):
        proc = _run(action, env=env)
        assert proc.returncode == 0, proc.stderr
    for line in _log(tmp_path):
        assert "--set-env-vars" not in line
        assert "--secrets" not in line
        assert "secret" not in line.lower()


@needs_tools
def test_off_scales_down_and_removes_the_tag(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    proc = _run("off", env=env)
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "0"
        assert state[app]["tag"] == ""


@needs_tools
def test_status_reports_on_when_every_app_is_tagged_in_the_future(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: on" in proc.stdout
    assert "approximate cost" in proc.stdout


@needs_tools
def test_status_reports_off_before_any_activation(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: off" in proc.stdout


@needs_tools
def test_status_reports_expired_when_the_tag_is_in_the_past(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    for app in APPS.values():
        state[app]["tag"] = "2000-01-01T00:00:00Z"
    state_path.write_text(json.dumps(state))
    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: expired" in proc.stdout


@needs_tools
def test_a_stuck_running_status_times_out(tmp_path: Path) -> None:
    env = _env(tmp_path, AZURE_DEMO_RUNNING_TIMEOUT_SECONDS="0", FAKE_AZ_RUNNING_SEQ="Waiting")
    proc = _run("on", env=env)
    assert proc.returncode == 1
    assert "did not reach Running" in proc.stderr


@needs_tools
def test_dry_run_prints_the_plan_and_touches_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for action in ("on", "off", "status"):
        proc = _run(action, "--dry-run", env=env)
        assert proc.returncode == 0, proc.stderr
        assert "would" in proc.stdout
    assert not (tmp_path / "az.json").exists()
    assert not (tmp_path / "az.log").exists()


@needs_tools
def test_missing_required_flags_are_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["AZURE_RESOURCE_GROUP"]
    proc = _run("on", env=env)
    assert proc.returncode != 0
    assert "--rg is required" in proc.stderr
