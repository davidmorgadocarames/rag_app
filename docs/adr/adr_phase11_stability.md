# ADR phase 11 — Stability: data persistence and rerank latency

- **Status:** Proposed (skeleton, 2026-09-27; completed at the 11a and 11b promotions)
- **Parts:** **11a** — persistence, migrations Job, asynchronous erasure (branch
  `phase-11a-persistence`); **11b** — rerank latency and baked model images (branch
  `phase-11b-latency`).
- **Related:** [ADR phase 6](adr_phase06_gdpr_erasure.md) (erasure, refined by 11a),
  [ADR phase 9](adr_phase09_deployment.md) (migrations at container start, superseded by
  11a), [ADR phase 10](adr_phase10_azure.md) (Azure deployment).

## Context

- Local development lost its data: `docker-compose.yml` declares the `pgdata` volume without
  a fixed name, so Docker prefixes it with the project folder and a clone under a different
  folder starts on an empty volume.
- On Azure, an existing account cannot read its own data (`InvalidToken` when its wrapped
  key is unwrapped with the running master key) — found during the Phase 10.5 smoke.
- `JWT_SECRET` and `DATA_MASTER_KEY` default to empty strings and nothing validates them.
- The backend image runs `alembic upgrade head` on every start (replica race, crash loop on
  a failed migration).
- Account erasure deletes the whole history inside the request, in one long transaction.
- The reranker model is loaded on every question.

## Diagnosis

*To be completed in 11.1:* local reproduction (volume names, row counts, exact commands);
Azure key check via `az containerapp exec` (OK/KO counts only) and its classification —
(a) data gone, (b) unreadable because the key changed, (c) session only; in-memory state
inventory (rate-limit buckets, signup tracker, singletons); the red `restart_check`.

## Decisions

*To be completed as 11a/11b land.* Planned sections:

1. Fixed development volume and an isolated gate project.
2. Fail-fast settings validation in the API lifespan; master-key fingerprint.
3. Migrations out of the container command: compose one-shot service and an Azure
   migration Job run by CD before the apps; expand/contract rule.
4. Slim `jobs` image for the migration, purge and backup Jobs.
5. Encrypted backups (14-day retention) with tombstones exported outside the database.
6. Asynchronous erasure: short request transaction (crypto-shred + PII scrub) → 202, then a
   batched, resumable purger.
7. Global daily answer cap.
8. (11b) One shared reranker, baked model images, latency gate.

## Rollback

*Written before each promotion.* 11a: previous backend digest with the command overridden to
uvicorn only; the purge Job is kept; the 0005 downgrade is not a rollback path.

## Costs

*Measured the day after each promotion* (Cost Management query) and compared with the caps:
11a ≈ €0.20 (cap €1), 11b ≈ €0.15 (cap €1).
