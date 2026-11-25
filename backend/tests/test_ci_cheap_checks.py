"""DA-16-C-1 (16-C-fix): the `manifests` gate step (kustomize build + kubeconform +
kube-linter, scripts/k8s/smoke-16-1.sh) must actually run in CI's `cheap-checks` job, not
only in a developer's local `--fast`/pre-push run. Before this fix, `cheap-checks`' own
`gate.sh --only ...` line never named `manifests` and the job never installed
kustomize/kubeconform/kube-linter at all -- a push that skipped the pre-push hook shipped a
kube-linter/manifest regression with zero server-side signal.

These tests parse `.github/workflows/ci.yml` directly, same pattern as
`test_ci_pins_the_same_shellcheck_as_the_local_installer` in `test_gate_script.py`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _cheap_checks_steps() -> list[dict]:
    ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    return ci["jobs"]["cheap-checks"]["steps"]


def _gate_only_step(steps: list[dict]) -> dict:
    return next(s for s in steps if "gate.sh --only" in s.get("run", ""))


def test_manifests_is_in_cheap_checks_gate_only_list() -> None:
    """DA-16-C-1 blocker: `manifests` must be in BOTH the step's own `name:` (so a human
    reading the Actions UI sees it ran) and the `run:` line actually passed to `gate.sh`."""
    steps = _cheap_checks_steps()
    gate_step = _gate_only_step(steps)
    assert "manifests" in gate_step.get("name", ""), (
        "cheap-checks' gate.sh step name does not mention 'manifests' -- "
        "DA-16-C-1 regression: the manifests step would silently stop running in CI"
    )
    run = gate_step["run"]
    assert "--only" in run
    only_list = run.split("--only", 1)[1].strip().split()[0].split(",")
    assert (
        "manifests" in only_list
    ), f"'manifests' missing from cheap-checks' gate.sh --only list: {only_list}"


def test_cheap_checks_installs_the_manifests_toolchain_before_the_gate_step() -> None:
    """kustomize/kubeconform/kube-linter must be installed (via the SAME installer
    `scripts/k8s/install-tools.sh` used locally -- one source of truth for pins/checksums,
    never a second hand-copied pin table) before `gate.sh --only ...,manifests` runs, or the
    `manifests` step would FAIL-closed on a missing tool (scripts/gate.sh step_manifests)."""
    steps = _cheap_checks_steps()
    install_step = next(s for s in steps if "install-tools.sh" in s.get("run", ""))
    run = install_step["run"]
    assert "install-tools.sh" in run and "scripts/k8s/install-tools.sh" in run
    for tool in ("kustomize", "kubeconform", "kubelinter"):
        assert tool in run, f"{tool} not passed to install-tools.sh in cheap-checks"
    assert "GITHUB_PATH" in run, "installed tools must be put on PATH for later steps"

    gate_step = _gate_only_step(steps)
    assert steps.index(install_step) < steps.index(
        gate_step
    ), "the toolchain install step must run BEFORE the gate.sh --only step"


def test_k8s_manifests_kind_job_is_unaffected_by_the_cheap_checks_fix() -> None:
    """DA-16-C-1's fix only touches `cheap-checks` -- the separate, free-runner
    `k8s-manifests-kind` job (real kind cluster + Kyverno Audit) must still exist, unchanged,
    and remain outside the 5 required status checks."""
    ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert "k8s-manifests-kind" in jobs
    kind_job = jobs["k8s-manifests-kind"]
    assert kind_job["name"] == "k8s manifests (kind cluster, Kyverno Audit, stub images)"
    assert kind_job["runs-on"] == "ubuntu-latest"
    # The kind job never calls scripts/k8s/install-tools.sh (it pins kind/kubectl/kustomize
    # inline, by design -- a kind cluster binary is not one of install-tools.sh's own tools);
    # this fix must not have bled cheap-checks' new step into it.
    kind_run_text = "\n".join(s.get("run", "") for s in kind_job["steps"])
    assert "install-tools.sh" not in kind_run_text
