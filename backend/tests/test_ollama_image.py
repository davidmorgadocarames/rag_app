"""Custom Ollama image hygiene (T11.4.3): `bge-m3` is pulled into the image's model store at
build time (server started, model pulled, server stopped — all in one layer) so a fresh
container embeds without ever pulling it over the network. Fixes the Azure `secrag-ollama`
ephemeral-storage loss documented in the runbook (gotcha 4) and ADR phase 11 decision 8.

The actual "embeds with networking disabled" proof is a real `docker build` + `docker run
--network none` (too heavy for pytest; done by hand and by CI's `ollama-image` job, ci.yml).
These are the static, fast guards against regressing the Dockerfile without re-running that
proof.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "ollama" / "Dockerfile"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONFIG_PY = REPO_ROOT / "backend" / "src" / "rag_app" / "config.py"


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_base_image_is_pinned_by_digest() -> None:
    """Never `ollama/ollama:latest` (today's Azure image, which floats) — a specific,
    reviewed release pinned by digest, same discipline as every other Dockerfile here."""
    match = re.search(r"^FROM\s+ollama/ollama@sha256:([0-9a-f]{64})\s*$", _text(), re.MULTILINE)
    assert match, "FROM must pin ollama/ollama by a 64-hex sha256 digest"


def test_embed_model_matches_the_app_settings() -> None:
    """The baked model name must be the exact one the backend asks Ollama for
    (`Settings.embed_model`) — a mismatch would mean the backend calls a model this image
    never pulled, so every embed call would still hit the network (defeating T11.4.3)."""
    text = _text()
    match = re.search(r"ARG EMBED_MODEL=(\S+)", text)
    assert match, "ARG EMBED_MODEL=<name> must be declared"
    baked_model = match.group(1)
    config_text = CONFIG_PY.read_text(encoding="utf-8")
    config_match = re.search(r'embed_model:\s*str\s*=\s*"([^"]+)"', config_text)
    assert config_match, "Settings.embed_model not found in config.py"
    assert baked_model == config_match.group(1)


def test_the_pull_step_starts_and_stops_its_own_server() -> None:
    """There is no "pull without a server" mode for Ollama: the RUN step must start `ollama
    serve`, wait for it, pull the model, then stop the server — all inside the SAME RUN
    instruction, so the pulled model blobs land in this layer (not lost when the step ends)."""
    text = _text()
    assert "ollama serve" in text
    assert "ollama pull" in text
    assert "$EMBED_MODEL" in text
    # The server must be stopped again (never left running into the next layer/the final image).
    assert "pkill" in text


def test_ci_has_an_ollama_image_job_that_proves_offline_embed() -> None:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "ollama-image" in jobs, "ci.yml must build+verify the Ollama image (T11.4.3)"
    job = jobs["ollama-image"]
    text = yaml.dump(job)  # covers `with:` fields too, not just `run:` scripts
    assert "ollama/Dockerfile" in text
    assert "--network none" in text
    assert "/api/embed" in text
    assert "bge-m3" in text


def test_ci_builds_the_ollama_image_with_the_gha_layer_cache() -> None:
    """DA-11bC-1 applies to this image too (block D note): without a cache, the ~1.1 GB
    `bge-m3` pull re-runs on every CI build even when the base digest and the model name
    both stayed the same."""
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    job = workflow["jobs"]["ollama-image"]
    steps = job["steps"]
    assert any(step.get("uses", "").startswith("docker/setup-buildx-action") for step in steps)
    build_step = next(step for step in steps if "ollama/Dockerfile" in yaml.dump(step))
    with_ = build_step.get("with", {})
    assert "type=gha" in with_.get("cache-from", "")
    assert "type=gha" in with_.get("cache-to", "")
