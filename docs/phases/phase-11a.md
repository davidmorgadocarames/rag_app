# Phase 11a — Stability: persistence, migrations Job, asynchronous erasure (report)

- **Closed:** 2026-10-02 · **Tag:** `phase-11a` (annotated, on `c8b4e9e`)
- **Promoted to Azure:** 2026-10-01 (fast-forward of `main` to `f584b30`), restore fixes
  promoted 2026-10-02 (`c8b4e9e`).
- **ADR:** [ADR phase 11 — Stability](../adr/adr_phase11_stability.md) (decisions 1–7, 9 and
  10 landed in 11a; decision 8 is 11b). ADR 6, 9 and 10 were amended where 11a supersedes them.
- **Why this phase existed:** local development had lost data (volume named by the compose
  project), migrations ran at container start, erasure was one long synchronous transaction,
  and there were no backups. 11a fixes persistence, deploys migrations as a Job, makes erasure
  asynchronous, adds encrypted backups and hardens the promotion.

## What was done

| Block | Rows | Result |
|---|---|---|
| A | −1, 1–4 | Pinned toolchain installer and `check.sh` (no sudo; PG 16 client, Node 22, Linux `gh`/`az`); toolchain floor in the gate; ADRs renamed to `adr_phaseNN_*`; phase-10.5 report |
| B | 5–8, 7b | `scripts/gate.sh` modes (`--fast` / `--full` / `--only`), isolated gate project `secrag-gate` (own volume, `127.0.0.1:15432`), pre-push hook, `adr-links`, `dependency-audit` with expiring exceptions, `schema-check`, a test-DB harness that refuses the development DB; CI with 5 jobs (incl. `cheap-checks` and a gitleaks history scan) |
| C | 9–12 | CD gated on the commit status `secrag/gate-full` (published only by a local `--full` PASS, creator checked); `ENV` setting with a dev-flag guard; `db/roles.sql` (`secrag_purger`, `secrag_backup`, client-side SCRAM verifiers); `migrations-roundtrip` gate step; compose backend reaches native Ollama |
| D | 13–17 | Diagnosis: local data intact in the old volume (not deleted), one account with a wrapped key that no longer unwrapped; `db-tunnel.sh` (temporary firewall rule, always removed), key-check snippet, `restart_check.sh` (red on the old compose) |
| E | 18–27 | External dev volume; fail-fast settings validation; migration `0005` (erasure tombstones, `purger_runs`, `usage_daily`, `master_key_fingerprint`); migrations as a compose one-shot and an Azure Job; slim `jobs` image; CD order images → migration Job → apps → Job images; `restart-check` gate step; offline key-recovery tool (`key_recovery.sh`) |
| F | 28–29 | `age`-encrypted backups (14-day retention) to Blob or a local folder, `restore.sh` that re-applies erasures from tombstone exports, `backup-pull.sh`, gate step `backup-drill` |
| G1 | 30–37 | `DELETE /account` → short transaction (crypto-shred + PII scrub) → 202; batched, resumable purger (advisory lock, `SKIP LOCKED`); tombstone exports with 30-day retention; gate step `erasure-scale`; account page |
| G2 | 37b–37d | Log hygiene (no address or link in logs, access-log query redaction, hidden SQL parameters); `/chat/stream` sends the conversation id first and always ends in `done` or `error`; dependency refresh (FastAPI/Starlette, sentence-transformers/transformers, pinned torch, pinned reranker revision) — no Python audit exceptions left |
| G fixes + 31b | — | Overdue-erasure warning, 503 + `Retry-After` on lock timeouts, sign-up race → 409, SSE keep-alive + `interrupted` marker; **global daily answer cap** (`DAILY_ANSWER_CAP`, race-safe reservation, 429 / `daily_cap_reached`), `max_tokens` on every LLM call |
| H | 38–39 | `BACKEND_SCHEMA` as implemented (0001–0005), APP_FLOW, README, DoD; rollback block and promotion checklist in ADR 11 |
| Pre-promotion fixes | — | Cap default 150 (code = Azure value), purge/backup Jobs created as Schedule Jobs with a dormant cron, `env … uvicorn` override for the old image, post-CD health gate |
| I | — | Azure environment moved from **Express** to a standard (workload profiles, Consumption) Container Apps environment — ADR 11 decision 10 |
| Post-promotion fixes | — | Found by the real-dump restore rehearsal: `7ee691a` (a SIGPIPE from `age` was reported as "wrong key") and `c8b4e9e` (restore from a filtered TOC list that skips Azure-managed extensions and their ACL entries) |

## Deviations from the plan (accepted by the user)

- **Row 31b omitted, caught in review.** The global daily answer cap (R6-1) was left out when
  block G1 was launched (rows listed as 30–37). The DA re-check of the G fixes caught it; it was
  implemented as its own task before block G closed.
- **Express environment blocker.** At the start of the promotion the Phase 10 environment
  turned out to be Container Apps **Express**: Jobs are not supported and
  `--revision-suffix` is rejected. The promotion was paused; the user chose to move all apps to
  one new standard environment (block I, outage ~20 min, app names kept). The old Express
  environment was deleted after the smoke.
- **No revision suffixes.** The plan's rollback block used timestamped `--revision-suffix`
  values; Express rejected them, so every `az containerapp update` is now written without a
  suffix (the tested form): Azure names each revision, and a repeated command never collides.
- **Measurement details.** `erasure-scale` uses three 100k-message users per run with a median
  criterion (a single Docker Desktop disk flush occasionally took 0.3–0.9 s); purge/backup Jobs
  are Schedule Jobs with a dormant cron (only the cron changes after CD); backup at 03:00 UTC;
  `done` tombstones are kept in the database for now (the purger has no `DELETE` on them).
- **CD GHCR `write_package` incident.** The first real CD run failed at "Build & push jobs
  image" (`denied: write_package`): the new `rag_app-jobs` package lacked Actions write access
  for the repository. Nothing had reached Azure; after the package access was fixed, re-running
  the failed jobs deployed normally.
- **Flaky timing test.** `tests/test_cd_pipeline.py:468` (`polls >= 2`) failed once on a
  branch push run (the PR run was green); a re-run passed.
- **Ruleset rejection.** The first fast-forward push of the restore fixes was rejected by the
  `main` ruleset (required `Backend` check failing because of that flaky test). After the
  re-run turned green the push went through — the ruleset worked as intended.

## Gate evidence

- Final `gate.sh --full` PASS **355 s** on `c8b4e9e` (eval 153 s, backup-drill 11 s); earlier
  promotion SHA `f584b30` PASS 417 s from the pre-push hook.
- **Eval** (unchanged since block G1): recall **1.0** · faithfulness **1.0** · correctness
  **0.9** · abstention **1.0**.
- **Erasure scale** (3 users × 100,000 messages, 8 workers): `DELETE /account` 202 in 44–71 ms
  (median 49 ms), 2,194 load requests with 0 failures / 5xx / lock errors / pool timeouts,
  pool peak 11 of 15; purger killed mid-run, next run purged 300,000 messages in 2.2 s, longest
  transaction 0.019 s.
- **Backup drill:** dump > 1 MiB (2.4 MB), restore from the offline key into an empty DB, the
  erased account stays erased (control without the export brings it back), wrong key and a
  non-dump file give distinct errors, Azure-shaped extension/ACL entries are skipped.
- **Restart check:** PASS (~60 s); red on the old compose (volume derived from the project).
- CI on `main`: ✓ 36922760218 (`f584b30`), ✓ 36938936771 (`c8b4e9e`).

## Azure promotion evidence

- **CD:** 36923407971 (`f584b30`, after the GHCR re-run) and 36939381497 (`c8b4e9e`); migration
  Job → **`0005_persistence_erasure`** (no-op on the second run).
- **Health gate:** backend Healthy, `/health` ok, `master_key_fingerprint` = 1 row,
  `DAILY_ANSWER_CAP=150`, no `ENV` variable, frontend 200, every app scale 0/1.
- **Crons:** purge `0 * * * *`, backup `0 3 * * *` (secrets and identity kept).
- **Smoke:** cited chat answer; stream cut after 4 s → first event `conversation`, then the
  "connection closed" marker; 3 throwaway accounts, warm `DELETE /account` **202 in 98 / 76 /
  75 ms** (incl. network), erased token → 401; purge Job (manual and scheduled) Succeeded, 3
  requests processed, 0 errors; backup Job Succeeded (encrypted dump + tombstone export in
  Blob), `backup-pull.sh` OK; Log Analytics: no email-like string and no unredacted `token=`.
- **Restore rehearsal** of the real Azure dump with the offline key (`c8b4e9e`): 155 TOC
  entries, 73 kept / 82 skipped (Azure-managed extensions, `pg_catalog` and default ACLs),
  restored counts **identical** to Azure (migration, users, keys, conversations, messages,
  tombstones, documents, chunks, fingerprint); role grants kept.

## Cost

Phase estimate ≈ €0.20 (cap €1). Measured (Azure Cost Management, whole subscription,
read-only): budget used **€6.10 of €40** up to 2026-10-01; daily **€0.37** on 2026-09-27,
**€0.03** on 2026-09-29, **€0.01** on 2026-10-01 (partial). The final figures for
2026-10-01/10-02 (promotion, block I and the smoke) are added at the next cost check, since
Cost Management lags by up to a day.

## Changed docs

[ADR 11](../adr/adr_phase11_stability.md) (new), [ADR 6](../adr/adr_phase06_gdpr_erasure.md),
[ADR 9](../adr/adr_phase09_deployment.md), [ADR 10](../adr/adr_phase10_azure.md),
[README](../../README.md), [Definition of Done](../DEFINITION_OF_DONE.md),
[BACKEND_SCHEMA](../BACKEND_SCHEMA.md), [APP_FLOW](../APP_FLOW.md), [PRD](../PRD.md),
[TRD](../TRD.md).

## Known follow-ups

- Ollama loses `bge-m3` on scale-to-zero (ephemeral storage) — wake-up procedure until baked
  model images in **11b (T11.4.3)**.
- Fix the flaky timing test `tests/test_cd_pipeline.py:468`.
- DA-G1-2: stop a purge Job mid-run under real load (nothing was pending during the smoke).
- Delete `done` tombstones older than ~60 days when the `retention` role arrives.
- Hashed lock file for Python dependencies (Phase 15).
- Audit the jobs-image pins in the gate, not only in CI (Phase 15).
