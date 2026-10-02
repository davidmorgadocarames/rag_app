"""docker-compose.yml hygiene (T11.0.11/T11.0.12/T11.0.13): loopback-only ports, native Ollama
by default (containerised one only in the `ci` profile), roles before the backend starts;
fixed external dev volume (T11.2.1), migrate one-shot + slim jobs image (T11.2.5, T11.2.12)."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_every_published_port_is_loopback_only(services: dict) -> None:
    published = {name: s.get("ports", []) for name, s in services.items()}
    assert published["db"] == ["127.0.0.1:5432:5432"]
    assert published["backend"] == ["127.0.0.1:8000:8000"]
    assert published["frontend"] == ["127.0.0.1:3000:3000"]
    assert published["ollama"] == ["127.0.0.1:11434:11434"]
    for ports in published.values():
        assert all(str(p).startswith("127.0.0.1:") for p in ports)


def test_metrics_port_is_never_published_to_the_host(services: dict) -> None:
    """T11.3.2/TF4: the backend's metrics port is compose-network-internal only (reachable
    by `prometheus`), never published to 127.0.0.1 like the API port is."""
    backend = services["backend"]
    assert backend["ports"] == ["127.0.0.1:8000:8000"]  # the API port only
    assert backend["expose"] == ["${METRICS_PORT:-9100}"]
    assert backend["environment"]["METRICS_PORT"] == "${METRICS_PORT:-9100}"


def test_prometheus_scrapes_the_metrics_port_with_7_day_retention_on_loopback(
    services: dict,
) -> None:
    """T11.3.3: image pinned by digest (X1), 7-day retention, UI on 127.0.0.1 only."""
    prom = services["prometheus"]
    assert prom["image"].split("@", 1)[0] == "prom/prometheus"
    assert prom["image"].split("@", 1)[1].startswith("sha256:")
    assert "--storage.tsdb.retention.time=7d" in prom["command"]
    assert prom["ports"] == ["127.0.0.1:9090:9090"]
    config = yaml.safe_load(
        (COMPOSE.parent / "deploy" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
    )
    targets = [t for job in config["scrape_configs"] for t in job["static_configs"][0]["targets"]]
    assert targets == ["backend:9100"]


def test_containerised_ollama_is_only_in_the_ci_profile(services: dict) -> None:
    assert services["ollama"]["profiles"] == ["ci"]
    dep = services["backend"]["depends_on"]["ollama"]
    assert dep == {"condition": "service_started", "required": False}


def test_backend_uses_the_native_ollama_by_default(services: dict) -> None:
    env = services["backend"]["environment"]
    assert env["OLLAMA_HOST"] == "${CONTAINER_OLLAMA_HOST:-http://host.docker.internal:11434}"
    assert "host.docker.internal:host-gateway" in services["backend"]["extra_hosts"]
    assert env["ENV"] == "${ENV:-prod}"  # fail-closed default; the root .env sets dev


def test_roles_run_before_the_backend(services: dict) -> None:
    roles = services["db-roles"]
    assert roles["entrypoint"] == ["bash", "/secrag/scripts/db/apply_roles.sh"]
    assert roles["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert services["backend"]["depends_on"]["db-roles"] == {
        "condition": "service_completed_successfully"
    }


def test_the_dev_volume_is_the_fixed_external_one() -> None:
    """T11.2.1: never a project-derived name again; compose never creates/removes it."""
    volumes = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["volumes"]
    assert volumes["pgdata"] == {"name": "rag_ia_pgdata", "external": True}


def test_migrate_one_shot_runs_after_the_roles_and_before_the_backend(services: dict) -> None:
    """T11.2.5: compose migrates once per `up`, from the jobs image, never the backend."""
    migrate = services["migrate"]
    assert migrate["build"] == {
        "context": "./backend",
        "dockerfile": "Dockerfile.jobs",
        "additional_contexts": {"dbscripts": "./scripts/db"},  # backup.sh (T11.2.10)
    }
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["depends_on"] == {
        "db": {"condition": "service_healthy"},
        "db-roles": {"condition": "service_completed_successfully"},
    }
    assert migrate["restart"] == "no"
    assert set(migrate["environment"]) == {"DATABASE_URL", "ENV"}  # no API secrets
    assert services["backend"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }


def _docker_ready() -> bool:
    if shutil.which("docker") is None or shutil.which("bash") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs a reachable docker")
def test_create_dev_volume_creates_once_and_never_recreates() -> None:
    """T11.2.1 "created once by a script" — on a throwaway volume name (test override)."""
    script = COMPOSE.parent / "scripts" / "dev" / "create_dev_volume.sh"
    name = f"secrag-cdv-test-{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "SECRAG_DEV_VOLUME": name, "SECRAG_STRAY_VOLUME": f"{name}-none"}

    def created_at() -> str:
        return subprocess.run(
            ["docker", "volume", "inspect", "-f", "{{.CreatedAt}}", name],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    try:
        first = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
        assert first.returncode == 0 and "created" in first.stdout, first.stderr
        before = created_at()
        second = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
        assert second.returncode == 0, second.stderr
        assert "exists" in second.stdout and "nothing to do" in second.stdout
        assert created_at() == before
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True)


def _cmd(dockerfile: str) -> str:
    lines = (COMPOSE.parent / "backend" / dockerfile).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith("CMD ")][-1]


def test_the_backend_image_never_migrates() -> None:
    cmd = _cmd("Dockerfile")
    assert "alembic" not in cmd and "uvicorn" in cmd


def test_the_jobs_image_is_slim_pinned_and_migrates() -> None:
    backend = COMPOSE.parent / "backend"
    dockerfile = (backend / "Dockerfile.jobs").read_text(encoding="utf-8")
    base = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(base) == 1 and "@sha256:" in base[0]  # base pinned by digest
    assert "torch" not in dockerfile.split("RUN pip install")[1]
    assert "postgresql-client-${PG_MAJOR}" in dockerfile and "ARG PG_MAJOR=16" in dockerfile
    assert " age" in dockerfile and "USER 10001" in dockerfile
    assert _cmd("Dockerfile.jobs") == 'CMD ["alembic", "upgrade", "head"]'


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.split("[")[0].strip().lower().replace("_", "-")] = version.strip()
    return pins


def test_jobs_requirements_are_pinned_like_the_backend_and_have_no_torch() -> None:
    backend = COMPOSE.parent / "backend"
    jobs = _pins(backend / "requirements-jobs.txt")
    runtime = _pins(backend / "requirements.txt")
    for heavy in ("torch", "sentence-transformers", "transformers", "fastapi", "uvicorn"):
        assert heavy not in jobs, heavy
    shared = set(jobs) & set(runtime)
    assert {"sqlalchemy", "alembic", "psycopg", "cryptography", "pydantic"} <= shared
    assert {name: jobs[name] for name in shared} == {name: runtime[name] for name in shared}
    assert {"azure-storage-blob", "azure-identity"} <= set(jobs)
