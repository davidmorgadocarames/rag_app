"""docker-compose.yml hygiene (T11.0.11/T11.0.12/T11.0.13): loopback-only ports, native Ollama
by default (containerised one only in the `ci` profile), roles before the backend starts."""

from __future__ import annotations

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
