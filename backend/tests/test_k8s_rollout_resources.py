"""T16.2.2 (resources from measurements) / T16.2.3 (zero-downtime rollouts) manifest-shape
assertions for ``deploy/k8s/base/{backend,frontend}``. Pure YAML parsing, no cluster needed --
wired into the ``pytest`` gate step.

Fails on the pre-block manifests (confirmed by checking out this block's base commit,
``acd56c6``, and re-running): no ``strategy``, no ``preStop``/``terminationGracePeriodSeconds``,
no ``PodDisruptionBudget``, and ``backend``'s ``resources.limits.memory`` was ``2Gi`` -- BELOW
this block's own measured idle VmRSS floor (~1.92-2.0 GiB), confirmed live to leave zero
headroom for the reranker's own startup load (measured VmHWM 2.93 GiB)."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_BASE = REPO_ROOT / "deploy" / "k8s" / "base"

# Matches base/gateway/httproute.yaml's SSE request/backendRequest timeout for
# POST /api/chat/stream, which both apps' termination grace period must outlive.
GATEWAY_SSE_TIMEOUT_S = 300


def _load_all(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _deployment(name: str) -> dict:
    docs = _load_all(K8S_BASE / name / "deployment.yaml")
    matches = [d for d in docs if d.get("kind") == "Deployment"]
    assert len(matches) == 1, f"expected exactly one Deployment in {name}/deployment.yaml"
    return matches[0]


def _pdb(name: str) -> dict:
    docs = _load_all(K8S_BASE / name / "pdb.yaml")
    matches = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    assert len(matches) == 1, f"expected exactly one PodDisruptionBudget in {name}/pdb.yaml"
    return matches[0]


def _container(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def test_both_pdb_files_exist_and_are_wired_into_their_kustomization() -> None:
    for name in ("backend", "frontend"):
        assert (K8S_BASE / name / "pdb.yaml").exists()
        kustomization = (K8S_BASE / name / "kustomization.yaml").read_text(encoding="utf-8")
        assert "pdb.yaml" in kustomization


class TestZeroDowntimeRollout:
    """T16.2.3: maxUnavailable: 0, preStop + a grace period that outlives the Gateway's own
    SSE timeout, PDBs."""

    def test_rolling_update_never_allows_an_unavailable_replica(self) -> None:
        for name in ("backend", "frontend"):
            strategy = _deployment(name)["spec"]["strategy"]
            assert strategy["type"] == "RollingUpdate"
            assert strategy["rollingUpdate"]["maxUnavailable"] == 0
            assert int(strategy["rollingUpdate"]["maxSurge"]) >= 1

    def test_termination_grace_period_outlives_the_gateway_sse_timeout(self) -> None:
        for name in ("backend", "frontend"):
            pod_spec = _deployment(name)["spec"]["template"]["spec"]
            grace = pod_spec["terminationGracePeriodSeconds"]
            pre_stop_cmd = _container(_deployment(name))["lifecycle"]["preStop"]["exec"]["command"]
            pre_stop_sleep = int(pre_stop_cmd[-1])
            assert grace >= pre_stop_sleep + GATEWAY_SSE_TIMEOUT_S, (
                f"{name}: terminationGracePeriodSeconds={grace} does not cover the "
                f"{pre_stop_sleep}s preStop sleep + the {GATEWAY_SSE_TIMEOUT_S}s SSE timeout"
            )

    def test_backend_uvicorn_graceful_shutdown_matches_the_grace_period_budget(self) -> None:
        """Without this, uvicorn's own shutdown window could be far shorter than the k8s-level
        grace period, making the manifest-level grace period cosmetic."""
        command = _container(_deployment("backend"))["command"]
        assert "--timeout-graceful-shutdown" in command
        idx = command.index("--timeout-graceful-shutdown")
        timeout_graceful_shutdown = int(command[idx + 1])
        pre_stop_sleep = int(
            _container(_deployment("backend"))["lifecycle"]["preStop"]["exec"]["command"][-1]
        )
        grace = _deployment("backend")["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]
        assert timeout_graceful_shutdown >= GATEWAY_SSE_TIMEOUT_S
        assert pre_stop_sleep + timeout_graceful_shutdown <= grace

    def test_topology_spread_constraints_are_soft_and_scoped_to_each_app(self) -> None:
        for name in ("backend", "frontend"):
            constraints = _deployment(name)["spec"]["template"]["spec"]["topologySpreadConstraints"]
            assert len(constraints) == 1
            c = constraints[0]
            assert c["whenUnsatisfiable"] == "ScheduleAnyway"
            assert c["labelSelector"]["matchLabels"] == {"app.kubernetes.io/name": name}

    def test_pdbs_block_voluntary_disruption_at_the_current_replica_count(self) -> None:
        for name in ("backend", "frontend"):
            pdb = _pdb(name)
            replicas = _deployment(name)["spec"]["replicas"]
            assert pdb["spec"]["minAvailable"] >= replicas
            assert pdb["spec"]["unhealthyPodEvictionPolicy"] == "AlwaysAllow"
            assert pdb["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": name}


class TestMeasuredResources:
    """T16.2.2: requests/limits set from this block's own measurements, never left at the
    pre-block placeholder numbers (backend's old `limits.memory: 2Gi` was BELOW the measured
    idle floor)."""

    def test_every_container_declares_both_requests_and_limits(self) -> None:
        for name in ("backend", "frontend"):
            resources = _container(_deployment(name))["resources"]
            assert resources["requests"]["cpu"]
            assert resources["requests"]["memory"]
            assert resources["limits"]["cpu"]
            assert resources["limits"]["memory"]

    @staticmethod
    def _mi(value: str) -> float:
        if value.endswith("Gi"):
            return float(value[:-2]) * 1024
        assert value.endswith("Mi"), value
        return float(value[:-2])

    @staticmethod
    def _millicores(value: str) -> int:
        return int(value[:-1]) if value.endswith("m") else int(value) * 1000

    def test_backend_memory_limit_is_at_or_above_the_measured_idle_floor(self) -> None:
        """This block measured backend idle VmRSS at 1.92-2.0 GiB -- a `limits.memory` below
        that (the pre-block manifest's `2Gi`, exactly at the floor with zero margin) leaves no
        room for the reranker's own startup load (measured VmHWM 2.93 GiB) and OOM-kills on
        the very first run."""
        resources = _container(_deployment("backend"))["resources"]
        limit_mi = self._mi(resources["limits"]["memory"])
        measured_idle_floor_mi = 2.0 * 1024
        assert limit_mi > measured_idle_floor_mi

    def test_requests_never_exceed_limits(self) -> None:
        for name in ("backend", "frontend"):
            resources = _container(_deployment(name))["resources"]
            assert self._millicores(resources["requests"]["cpu"]) <= self._millicores(
                resources["limits"]["cpu"]
            )
            assert self._mi(resources["requests"]["memory"]) <= self._mi(
                resources["limits"]["memory"]
            )

    def test_backend_pins_torch_threads_to_match_its_own_cpu_limit(self) -> None:
        """Self-reported finding: a k8s CPU `limit` is a CFS quota, not a cpuset -- without
        pinning torch's own thread pool it sizes itself from the node's full core count, not
        the pod's quota, and throttles hard under load."""
        config = yaml.safe_load(
            (K8S_BASE / "backend" / "configmap.yaml").read_text(encoding="utf-8")
        )
        cpu_limit_cores = (
            int(_container(_deployment("backend"))["resources"]["limits"]["cpu"][:-1]) / 1000
        )
        assert int(config["data"]["OMP_NUM_THREADS"]) == int(cpu_limit_cores)
        assert int(config["data"]["MKL_NUM_THREADS"]) == int(cpu_limit_cores)
