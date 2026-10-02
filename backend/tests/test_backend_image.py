"""Backend image hygiene (T11.4.2): the cross-encoder reranker is baked into the image's
Hugging Face cache at build time, at config.py's PINNED revision — never a separate ARG that
could drift from it — and ``HF_HUB_OFFLINE=1`` is set only AFTER that download layer (it still
needs the network). The actual "reranks with networking disabled" proof is a real `docker
build` + `docker run --network none` (too heavy for pytest; done by hand and by CI's
``backend-image`` job, ci.yml) — these are the static, fast guards against regressing either
file without re-running that proof.

DA-11bC-1: the download layer must depend ONLY on the two files ``get_settings()`` actually
needs (``rag_app/__init__.py``, ``rag_app/config.py``), copied BEFORE the full ``COPY src
./src`` — not on the rest of ``src/``, which changes on nearly every commit. Docker's layer
cache is sequential (an earlier cache miss invalidates every later layer regardless of its
own inputs), so getting this order wrong silently turns "bake the model once" into
"re-download the ~2.1-2.3 GB snapshot on every code-only build" again, in both a local
``docker build`` and CI. Confirmed live (PHASE_STATUS block D): a code-only change elsewhere
under ``src/`` reuses the cached download layer; a ``reranker_model``/``reranker_revision``
change or a change to either copied file does not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONFIG_PY = REPO_ROOT / "backend" / "src" / "rag_app" / "config.py"
INIT_PY = REPO_ROOT / "backend" / "src" / "rag_app" / "__init__.py"
CONFIG_PY = REPO_ROOT / "backend" / "src" / "rag_app" / "config.py"
INIT_PY = REPO_ROOT / "backend" / "src" / "rag_app" / "__init__.py"


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


def test_download_layer_copies_only_the_two_files_config_needs_before_the_full_tree() -> None:
    """DA-11bC-1: a narrow ``COPY`` of just ``__init__.py``/``config.py`` must happen BEFORE
    the download RUN step, and the full ``COPY src ./src`` must happen AFTER it — otherwise
    every code-only commit (which touches something else under ``src/``) would invalidate the
    download layer again, defeating the whole point of baking the model."""
    lines = _lines()

    def is_narrow_copy(line: str) -> bool:
        return (
            line.startswith("COPY")
            and "src/rag_app/__init__.py" in line
            and "src/rag_app/config.py" in line
        )

    narrow_copy_idx = next(i for i, line in enumerate(lines) if is_narrow_copy(line))
    download_idx = next(i for i, line in enumerate(lines) if "snapshot_download" in line)
    full_copy_idx = next(i for i, line in enumerate(lines) if line == "COPY src ./src")
    assert narrow_copy_idx < download_idx < full_copy_idx


def test_config_module_does_not_import_other_rag_app_modules() -> None:
    """The Dockerfile's narrow COPY (above) is only safe as long as ``config.py`` (and the
    package ``__init__.py``) import nothing else from ``rag_app`` — otherwise
    ``from rag_app.config import get_settings`` would fail at build time with a clear
    ``ModuleNotFoundError`` for files the narrow COPY never copied in. A static AST check
    instead of relying on the build to fail: a future contributor who adds such an import to
    ``config.py`` finds out here, in a two-second test, not after a multi-minute Docker build."""
    msg = "the Dockerfile's narrow COPY would miss it"
    for path in (INIT_PY, CONFIG_PY):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                bad = node.module.startswith("rag_app")
                assert not bad, f"{path.name} imports {node.module} — {msg}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bad = alias.name.startswith("rag_app.")
                    assert not bad, f"{path.name} imports {alias.name} — {msg}"


def test_ci_has_a_backend_image_job_that_proves_offline_rerank() -> None:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "backend-image" in jobs, "ci.yml must build+verify the backend image (T11.4.2)"
    job = jobs["backend-image"]
    # Dumped back to text rather than only joining `run:` scripts — the build step itself is
    # a `uses:`/`with:` action (DA-11bC-1 caching), not a `run:` docker build command.
    text = yaml.dump(job)
    assert "backend/Dockerfile" in text
    assert "--network none" in text
    assert "HF_HUB_OFFLINE" in text


def test_ci_builds_the_backend_image_with_the_gha_layer_cache() -> None:
    """DA-11bC-1: without a registry/GHA cache, the ~2.1-2.3 GB baked-model layer re-downloads
    on every CI run even when neither the pin nor the files it depends on changed (the local
    Dockerfile reorder alone only helps a local, long-lived Docker daemon)."""
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    job = workflow["jobs"]["backend-image"]
    steps = job["steps"]
    assert any(step.get("uses", "").startswith("docker/setup-buildx-action") for step in steps)
    build_step = next(step for step in steps if "backend/Dockerfile" in yaml.dump(step))
    with_ = build_step.get("with", {})
    assert "type=gha" in with_.get("cache-from", "")
    assert "type=gha" in with_.get("cache-to", "")
