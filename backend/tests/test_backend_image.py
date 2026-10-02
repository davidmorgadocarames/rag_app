"""Backend image hygiene (T11.4.2): the cross-encoder reranker is baked into the image's
Hugging Face cache at build time, at config.py's PINNED revision — never a separate ARG that
could drift from it — and ``HF_HUB_OFFLINE=1`` is set only AFTER that download layer (it still
needs the network). The actual "reranks with networking disabled" proof is a real `docker
build` + `docker run --network none` (too heavy for pytest; done by hand and by CI's
``backend-image`` job, ci.yml) — these are the static, fast guards against regressing either
file without re-running that proof.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _lines() -> list[str]:
    """Dockerfile lines with full-line comments stripped out — a prose comment can mention
    the same strings the directives below use, so only instruction lines should match."""
    return [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]


def test_dockerfile_has_no_separate_pinned_reranker_arg() -> None:
    """The baked snapshot must read config.py's Settings (single source of truth) — a
    separate ``ARG RERANKER_REVISION=...`` would duplicate the pin and could silently drift
    from what the running app actually asks for."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG RERANKER_REVISION" not in text
    assert "ARG RERANKER_MODEL" not in text


def test_dockerfile_download_layer_reads_the_real_settings() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "snapshot_download" in text
    assert "from rag_app.config import get_settings" in text
    assert "s.reranker_model" in text and "s.reranker_revision" in text


def test_dockerfile_sets_hf_hub_offline_only_after_the_download_layer() -> None:
    """``HF_HUB_OFFLINE=1`` must come AFTER the snapshot-download RUN step: that step still
    needs the network to fetch the pinned snapshot; setting it earlier would break the build."""
    lines = _lines()
    download_idx = next(i for i, line in enumerate(lines) if line.startswith("RUN python -c"))
    offline_idx = next(i for i, line in enumerate(lines) if line.startswith("ENV HF_HUB_OFFLINE"))
    assert offline_idx > download_idx


def test_dockerfile_still_installs_cpu_torch_before_the_download() -> None:
    """The download layer imports ``rag_app.config`` (pydantic-settings) and
    ``huggingface_hub`` — both already installed by the earlier ``pip install`` layer, never
    re-installed just for this RUN step."""
    lines = _lines()
    pip_idx = next(i for i, line in enumerate(lines) if "pip install" in line and "torch" in line)
    download_idx = next(i for i, line in enumerate(lines) if "snapshot_download" in line)
    assert download_idx > pip_idx


def test_ci_has_a_backend_image_job_that_proves_offline_rerank() -> None:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "backend-image" in jobs, "ci.yml must build+verify the backend image (T11.4.2)"
    job = jobs["backend-image"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "backend/Dockerfile" in runs
    assert "--network none" in runs
    assert "HF_HUB_OFFLINE" in runs
