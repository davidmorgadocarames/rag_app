# SecRAG backend (FastAPI). Build context: ./backend
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; torch is installed CPU-only (pinned, from its own index) to keep
# the image small, then the runtime pins from PyPI.
COPY requirements-torch.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-torch.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

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
RUN python -c "\
from huggingface_hub import snapshot_download; \
from rag_app.config import get_settings; \
s = get_settings(); \
snapshot_download(s.reranker_model, revision=s.reranker_revision)"

ENV HF_HUB_OFFLINE=1

EXPOSE 8000

# The API only. Migrations never run at container start (T11.2.5): locally the compose one-shot
# `migrate` runs them before the backend starts; on Azure the migration Job, started by CD
# before the apps are updated. The lifespan refuses to start on invalid settings or a
# master-key fingerprint mismatch (T11.2.2, T11.2.4).
CMD ["uvicorn", "rag_app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
