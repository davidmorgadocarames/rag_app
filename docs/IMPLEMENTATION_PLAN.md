# Implementation Plan — SecRAG

Step-by-step build sequence. Each phase is a small, shippable increment that keeps CI green and, from
Phase 4 on, must pass the evaluation gate before deploy. Phases map to the roadmap in the README.

## Phase 0 — Foundation (this commit) ✅

- Six docs (PRD, TRD, App Flow, UI/UX, Backend Schema, this plan).
- Monorepo skeleton: `backend/` (FastAPI + pydantic-settings), `frontend/` (Next.js/TS/Tailwind).
- `venv` + pinned deps; pre-commit (ruff, ruff-format, gitleaks); green CI (backend + frontend + secret
  scan); `.env.example`; dev `docker-compose` (Postgres+pgvector, Ollama).
- **Done when**: `pre-commit run --all-files`, backend checks, and frontend build all pass locally and
  in CI.

## Phase 1 — Corpus & ingestion

- Collect the mini OWASP corpus (Top 10 2021 + 2025, a few Cheat Sheets incl. SQL & command injection).
- Ingestion: PDF → Markdown (layout-aware), write to `data/markdown/`.
- Chunk on semantic boundaries with overlap; attach metadata (`source`, `version`, `effective_date`,
  `heading`).
- **Done when**: chunks are inspectable Markdown and a script prints chunk stats.

## Phase 2 — Embeddings, storage & retrieval

- Alembic migrations for `documents` + `chunks` (pgvector + tsvector).
- Embed with `bge-m3`; store vectors; build hybrid retrieval (pgvector ANN + BM25).
- Add cross-encoder rerank (`bge-reranker`).
- **Done when**: a CLI query returns ranked chunks with scores.

## Phase 3 — Generation, citations & abstention

- Generate answers grounded in retrieved chunks, with citations (doc/version/date).
- Groundedness check; abstain when unsupported or thin.
- **Done when**: end-to-end CLI Q&A returns cited answers and abstains on out-of-corpus questions.

## Phase 4 — Evaluation & the deploy gate

- Build the golden set (from `eval/golden_set.example.jsonl`), including the negative set.
- Ragas: retrieval metrics (deterministic, every push) + generation metrics + **correctness vs truth**
  (PR/nightly). Validate the LLM-judge against human labels.
- `baseline_metrics.json` + regression gate as a CI stage that **blocks deploy** below thresholds.
- Benchmark harness (precision / cost / p95 latency) across configs.
- **Done when**: CI fails on a deliberate quality regression.

## Phase 5 — Agentic router (only if justified)

- Add router (no-retrieval / single-hop / multi-hop / reformulate+retry / abstain).
- Keep it **only if** the benchmark shows better correctness without unacceptable cost/latency.
- **Done when**: benchmark table documents the decision.

## Phase 6 — Auth & abuse defense

- Implement `users`, tokens, sessions per Backend Schema; argon2; email verification.
- Rate limiting (token bucket, cost-aware); signup risk scoring; per-user quotas.
- **Done when**: auth flows work and rate limits are enforced with tests.

## Phase 7 — Frontend screens

- Landing, signup/login/verify, chat (with citation chips + abstention card), history, account.
- Wire to backend API; handle loading/empty/abstention/429/error states.
- **Done when**: full user journey works in the browser.

## Phase 8 — Compliance (crypto-shred)

- Per-user encryption key (`user_keys`); encrypt user content at rest.
- Account deletion erases relational rows, vectors, caches, and traces.
- **Done when**: deleting a user leaves no recoverable user data anywhere.

## Phase 9 — Containerization & deploy (CD)

- Dockerfiles for backend/frontend; full `docker-compose`.
- GitHub Actions: test + eval gate → build → push (GHCR) → deploy to free tier.
- **Done when**: a merge to `main` deploys automatically after passing the gate.

## Phase 10 — Cloud deployment (Azure)

- Add the `ChatClient` interface + `AzureOpenAIChat`; `LLM_PROVIDER` setting, default `ollama`.
- Provision Azure Database for PostgreSQL Flexible Server; enable `pgvector`; run
  `alembic upgrade head` against it.
- Provision two Azure Container Apps (backend, frontend) pulling from the existing GHCR
  images; wire env vars/secrets (`DATABASE_URL`, `JWT_SECRET`, `DATA_MASTER_KEY`,
  `LLM_PROVIDER=azure_openai`, `AZURE_OPENAI_*`, `NEXT_PUBLIC_API_URL`).
- Extend `cd.yml` with the Azure deploy step (OIDC, no stored secrets).
- **Done when**: a merge to `main` deploys automatically to a public Azure URL, the eval
  gate still blocks a bad deploy exactly as it does for Phase 9, and a real question
  answered end-to-end through the public URL returns a grounded, cited answer (or a
  correct abstention).

## Cross-cutting (every phase)

- No secrets committed; config via env; deps pinned.
- Structured tracing on every pipeline stage (find the slow/failing step).
- Tests added with each feature; CI stays green.
