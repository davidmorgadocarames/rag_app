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
from datetime import UTC, datetime, timedelta
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
        fail_for = os.environ.get("FAKE_AZ_FAIL_TAG_MERGE_FOR", "").split(",")
        if app in fail_for:
            sys.exit(1)
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

# FAKE_CURL_STATUS lets a test make the final /health check fail (default: 200, healthy).
FAKE_CURL = "#!/bin/sh\nprintf '%s' \"${FAKE_CURL_STATUS:-200}\"\n"


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


# --- DA-11bH-1: `status` must aggregate across ALL THREE apps, not latch on the first
# app that looks awake. Red on the pre-fix code: it only ever set overall="on" once and
# never downgraded it for a later app with min=0, so a mixed state (e.g. Ollama asleep
# while backend/frontend are awake) was misreported as "on". ---


@needs_tools
def test_status_reports_partial_when_one_app_is_asleep_and_the_others_are_on(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    # put Ollama back to sleep by hand (e.g. demo-guard.sh scaled it, or a manual edit),
    # leaving backend/frontend awake with a still-future tag.
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    state[APPS["ollama"]]["min"] = "0"
    state[APPS["ollama"]]["tag"] = ""
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: partial" in proc.stdout
    assert "state: on" not in proc.stdout


@needs_tools
def test_status_reports_partial_not_on_for_any_mix_of_awake_and_asleep_apps(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    # two apps asleep, only the backend awake.
    for app in (APPS["ollama"], APPS["frontend"]):
        state[app]["min"] = "0"
        state[app]["tag"] = ""
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: partial" in proc.stdout
    assert "state: on" not in proc.stdout


@needs_tools
def test_status_reports_expired_even_when_other_apps_are_cleanly_off(tmp_path: Path) -> None:
    """A min=1 app with a bad tag must force "expired", even if every other app is
    cleanly asleep (min=0) — a bad tag is a problem demo-guard.sh needs to clean up, not
    a "partial"/"off" mix."""
    env = _env(tmp_path)
    state_path = tmp_path / "az.json"
    state = {
        APPS["backend"]: {"min": "1", "tag": "", "running": "Running"},
        APPS["ollama"]: {"min": "0", "tag": "", "running": "Running"},
        APPS["frontend"]: {"min": "0", "tag": "", "running": "Running"},
    }
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: expired" in proc.stdout


# --- DA-11bH-2: `on` must not leave apps up with a valid future tag if the wait/Running
# check or the final /health check fails: on such a failure it must roll back (every app
# it touched back to min-replicas 0, every tag it wrote removed) and exit non-zero. ---


@needs_tools
def test_on_rolls_back_when_the_running_check_fails_after_tagging(tmp_path: Path) -> None:
    env = _env(tmp_path, AZURE_DEMO_RUNNING_TIMEOUT_SECONDS="0", FAKE_AZ_RUNNING_SEQ="Waiting")
    proc = _run("on", env=env)
    assert proc.returncode != 0
    assert "did not reach Running" in proc.stderr
    assert "rolled back" in proc.stderr or "rolling back" in proc.stdout
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "0", f"{app} must be rolled back to min-replicas 0"
        assert state[app]["tag"] == "", f"{app} must not keep a tag after a rolled-back 'on'"


@needs_tools
def test_on_rolls_back_when_the_final_health_check_fails(tmp_path: Path) -> None:
    env = _env(tmp_path, AZURE_DEMO_HEALTH_TIMEOUT_SECONDS="0", FAKE_CURL_STATUS="500")
    proc = _run("on", env=env)
    assert proc.returncode != 0
    assert "rolled back" in proc.stderr or "rolling back" in proc.stdout
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "0", f"{app} must be rolled back to min-replicas 0"
        assert state[app]["tag"] == "", f"{app} must not keep a tag after a rolled-back 'on'"


@needs_tools
def test_on_rolls_back_the_apps_already_scaled_when_tagging_fails(tmp_path: Path) -> None:
    """Failure "after scaling": every app is already at min-replicas 1 (the scale loop
    ran to completion) when the FIRST tag write fails -- the two already-scaled apps must
    still be rolled back, even though no tag was ever written for any of them."""
    env = _env(tmp_path, FAKE_AZ_FAIL_TAG_MERGE_FOR=APPS["ollama"])
    proc = _run("on", env=env)
    assert proc.returncode != 0
    assert "rolled back" in proc.stderr or "rolling back" in proc.stdout
    state = _state(tmp_path)
    for app in APPS.values():
        assert state[app]["min"] == "0", f"{app} must be rolled back to min-replicas 0"
        assert state[app]["tag"] == "", f"{app} must not keep a tag after a rolled-back 'on'"


@needs_tools
def test_on_never_restarts_an_already_active_app_only_extends_the_tag_still_holds_after_h2(
    tmp_path: Path,
) -> None:
    """Guard against a regression where the rollback/do_on refactor (block H2) would make
    `on` re-scale an already min-replicas=1 app."""
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    updates_before = sum(1 for line in _log(tmp_path) if line.startswith("containerapp update"))
    assert _run("on", env=env).returncode == 0
    updates_after = sum(1 for line in _log(tmp_path) if line.startswith("containerapp update"))
    assert updates_after == updates_before


# --- F-H-1: `status`'s "approximate cost so far" assumed start = tag - 3h, which is only
# true while the window is genuinely open. A hand-edited or long-expired tag made that
# assumption produce a nonsense figure (e.g. EUR 1142.30 for a tag set to 2026-01-01). The
# estimate must be capped at the 3h window and shown only for a reliably-open ("on") demo;
# any other state (expired/partial/off) or a malformed tag must print "n/a" instead. ---


@needs_tools
def test_status_cost_is_approximately_zero_right_after_on(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: on" in proc.stdout
    assert "approximate cost so far: EUR 0.00" in proc.stdout


@needs_tools
def test_status_cost_is_sane_for_a_future_tag_partway_through_the_window(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    # 1h left on the 3h window => ~2h elapsed => cost ~= 2 * HOURLY_COST_EUR.
    one_hour_from_now = datetime.now(UTC) + timedelta(hours=1)
    tag_value = one_hour_from_now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for app in APPS.values():
        state[app]["tag"] = tag_value
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: on" in proc.stdout
    expected = 2 * float(os.environ.get("DEMO_MODE_HOURLY_COST_EUR", "0.17"))
    assert f"approximate cost so far: EUR {expected:.2f}" in proc.stdout


@needs_tools
def test_status_cost_is_na_for_a_tag_expired_far_in_the_past(tmp_path: Path) -> None:
    """Red on the pre-fix code: a tag hand-edited (or left) far in the past made
    `elapsed = now - (tag - 3h)` huge, printing a nonsense cost like EUR 1142.30 instead of
    a capped or "n/a" figure."""
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    for app in APPS.values():
        state[app]["tag"] = "2026-01-01T00:00:00Z"
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: expired" in proc.stdout
    assert "approximate cost so far: n/a" in proc.stdout
    assert "EUR" not in proc.stdout


@needs_tools
def test_status_cost_is_na_for_a_malformed_tag(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    for app in APPS.values():
        state[app]["tag"] = "not-a-date"
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: expired" in proc.stdout
    assert "approximate cost so far: n/a" in proc.stdout
    assert "EUR" not in proc.stdout


@needs_tools
def test_status_cost_is_na_for_partial_state(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _run("on", env=env).returncode == 0
    state_path = tmp_path / "az.json"
    state = json.loads(state_path.read_text())
    state[APPS["ollama"]]["min"] = "0"
    state[APPS["ollama"]]["tag"] = ""
    state_path.write_text(json.dumps(state))

    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: partial" in proc.stdout
    assert "approximate cost so far: n/a" in proc.stdout
    assert "EUR" not in proc.stdout


@needs_tools
def test_status_cost_is_na_before_any_activation(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = _run("status", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "state: off" in proc.stdout
    assert "approximate cost so far: n/a" in proc.stdout
    assert "EUR" not in proc.stdout
