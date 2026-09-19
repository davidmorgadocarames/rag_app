# ADR 0003 — Deployment (containers + CD to GHCR)

- **Status:** Accepted (2026-09-19)
- **Context:** Phase 9. The CI/CD-first goal needs the app packaged and shipped
  automatically. But the RAG depends on **Ollama with a GPU** (qwen generation +
  bge-m3 embeddings), which typical free tiers do not provide, and the backend image
  carries torch (for the reranker) — so a naive "deploy everything to a free tier" is
  not realistic.

## Decision

- **Containerize everything:** `backend/Dockerfile` (FastAPI; torch installed CPU-only
  to keep the image small; runs `alembic upgrade head` then uvicorn) and
  `frontend/Dockerfile` (Next.js standalone). `docker-compose.yml` runs the whole stack
  (db + ollama + backend + frontend).
- **CD publishes images to GHCR** on every push to `main` (`.github/workflows/cd.yml`,
  using the built-in `GITHUB_TOKEN` — no external account needed). This is the
  automated, working CD.
- **Host deployment is a separate, credentialed step**, deliberately not wired to a
  specific provider in CI.

## Deployment topology (recommended)

The web tiers are stateless and cheap; the model tier needs a GPU:

- **Frontend + backend + Postgres**: any container host / free tier (Railway, Render,
  Fly.io). Set `DATABASE_URL`, `JWT_SECRET`, `DATA_MASTER_KEY`, `FRONTEND_ORIGIN`, and
  point `OLLAMA_HOST` at the model tier. Build the frontend with the public
  `NEXT_PUBLIC_API_URL`.
- **Model tier (Ollama)**: a GPU host (a GPU VM, or the developer's machine exposed via
  a tunnel). Free tiers without GPUs can't run qwen at usable latency.

To deploy: pull `ghcr.io/davidmorgadocarames/rag_app-{backend,frontend}:latest`, provide
the env above, and run behind the platform. The pre-push **eval gate** already guarantees
quality before anything ships.

## Backups & retention

Postgres backups follow the retention/erasure policy in
[ADR 0002](0002-data-erasure-gdpr.md): put beyond use, 7-day rotation, replay deletions
on restore.

## Consequences

- CD is real and free (images on GHCR) without over-promising a GPU-less free-tier deploy.
- Swapping the model tier for a hosted LLM later only changes `OLLAMA_HOST` / the LLM
  client — the rest is unaffected.
