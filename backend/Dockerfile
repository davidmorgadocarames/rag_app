# SecRAG backend (FastAPI). Build context: ./backend
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; torch is installed CPU-only (pinned, from its own index) to keep
# the image small, then the runtime pins from PyPI.
COPY requirements-torch.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-torch.txt \
    && pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app/src

# T11.4.2: bake the cross-encoder reranker into this image's Hugging Face cache at build
# time, at config.py's PINNED reranker_model/reranker_revision — reading the SAME Settings
# the app reads at runtime, so the baked snapshot can never drift from what the running app
# asks for (single source of truth, no separate ARG to keep in sync; test_backend_
# dockerfile.py guards this file against ever reintroducing one). HF_HUB_OFFLINE=1 (set only
# AFTER this download layer, which still needs the network) then means every load — the
# lifespan's warm-up (T11.4.1) and any fallback construction — comes from this cache only,
# never the network, even on a cold start with no egress: this also removes the Hugging Face
# Hub as a runtime supply-chain surface (DA-B-7) for the deployed container.
#
# DA-11bC-1: this layer COPIES ONLY the two files `get_settings()` actually needs
# (`rag_app/__init__.py`, `rag_app/config.py` — neither imports any other `rag_app` module,
# guarded by test_backend_image.py) instead of the whole `src/` tree. Docker's layer cache is
# sequential: an earlier `COPY src ./src` (which changes on nearly every commit) would have
# invalidated this ~2.1-2.3 GB download on every ordinary code-only change, in both a local
# `docker build` and CI's `backend-image` job (no registry/GHA cache is configured there
# either — see ci.yml). Ordinary commits now reuse this layer from cache; only a
# `reranker_model`/`reranker_revision` bump (or a change to one of these two files) re-runs
# the download.
COPY src/rag_app/__init__.py src/rag_app/config.py ./src/rag_app/
# ignore_patterns (T11.5.3, 11b block F) excludes redundant weight formats some Hub repos
# ship alongside safetensors (bge-reranker-base's own repo carries BOTH a 1.1 GB
# pytorch_model.bin AND a 1.1 GB model.safetensors for the SAME weights, plus an onnx/
# export this repo never uses, per decision 8 -- transformers/sentence_transformers already
# prefer safetensors automatically when both exist) -- found by actually inspecting this
# image's baked HF cache after adopting a new model, not assumed; v2-m3's own repo only ever
# shipped safetensors, so this was invisible before.
RUN python -c "\
from huggingface_hub import snapshot_download; \
from rag_app.config import get_settings; \
s = get_settings(); \
snapshot_download(s.reranker_model, revision=s.reranker_revision, ignore_patterns=['*.bin', '*.h5', '*.ot', '*.msgpack', 'onnx/*', '*.onnx'])"

ENV HF_HUB_OFFLINE=1

# The rest of the source tree: changes on nearly every commit, but never invalidates the
# download layer above (already run, from the smaller COPY).
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

EXPOSE 8000

# The API only. Migrations never run at container start (T11.2.5): locally the compose one-shot
# `migrate` runs them before the backend starts; on Azure the migration Job, started by CD
# before the apps are updated. The lifespan refuses to start on invalid settings or a
# master-key fingerprint mismatch (T11.2.2, T11.2.4).
CMD ["uvicorn", "rag_app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
