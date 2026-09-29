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

1. Fixed development volume and an isolated gate project. *Landed in 11.0 (gate part):*
   `compose.gate.yml` (project `secrag-gate`, volume `secrag_gate_pgdata`, DB on
   `127.0.0.1:15432` — the planned 55432 sits in a Hyper-V reserved port block on the
   development machine), seeded from a git-ignored dump (`gate.sh --make-seed`) and removed
   with `down -v` after every `--full`; a test DB harness that refuses the development
   database; see [Definition of Done — Gate modes](../DEFINITION_OF_DONE.md#gate-modes).
   *Landed in 11.0 (CD part):* CD runs on `workflow_run` of CI on `main` (success only),
   uses the CI run's `head_sha` everywhere, compares the changed files with the last
   deployed SHA (GitHub Deployments; only `docs/**`, `**/*.md`, `deploy/k8s/**` → skip),
   deploys images by digest, and refuses any SHA without the commit status
   `secrag/gate-full` = success that a local `gate.sh --full` PASS publishes for exactly that
   SHA (D-2026-09-27-7 b) — a skipped pre-push hook can no longer reach Azure. CI runs can
   finish out of order (a re-run of an old commit), so the plan also skips a SHA that is no
   longer the tip of `main` at plan time (its successor's run deploys) and a SHA that equals
   or is an ancestor of the last deployed one — CD never rolls back. A real manual dispatch
   (`dry_run=false` on `main`) additionally needs a successful CI `push` run for exactly that
   SHA on `main` (`scripts/cd/ci_status.sh`, D-2026-09-29-1 a). No
   `environment:` key (OIDC subject unchanged); dry runs stay echo-only.
   *Compose ↔ native Ollama (T11.0.11/12, input for 16.0):* the backend container calls
   `host.docker.internal:11434`; Docker Desktop forwards it to Windows' loopback and WSL2's
   default localhost forwarding (NAT mode, no `.wslconfig`) relays it to Ollama bound to
   `127.0.0.1` inside WSL. Measured on the development machine: Windows listens only on
   `127.0.0.1:11434` (`wslrelay`), so there is no LAN exposure, no firewall rule, no mirrored
   networking and no distro-IP script (the planned options; not needed). Verified with a
   throw-away compose project: the backend container listed `bge-m3` and `qwen2.5` at
   `/api/tags` and answered one grounded chat question (4 citations); `ss -ltn` showed only
   `127.0.0.1` listeners. Dependencies: Docker Desktop and `localhostForwarding` (default on);
   for k3d (Phase 16) the same path is `host.docker.internal` / `host.k3d.internal`. The
   containerised Ollama moved to the `ci` profile; every published port is on `127.0.0.1`.
   *Roles (T11.0.13):* idempotent `db/roles.sql` (`secrag_purger`, `secrag_backup`, NOLOGIN;
   default privileges so every future table stays dumpable), applied before every migrate
   (compose `db-roles`, CI, gate, test harness); LOGIN + passwords outside Alembic
   (`scripts/db/apply_roles.sh` via `\getenv`). The server only ever receives a SCRAM-SHA-256
   verifier computed client-side (`scripts/db/scram_verifier.pl`, RFC 5802/7677), never the
   plaintext, so no server log setting (`log_statement`, failing-statement logging) can leak
   a role password. Gate step `migrations-roundtrip`.
2. Fail-fast settings validation in the API lifespan; master-key fingerprint. *Landed in 11.0:*
   `ENV` (`dev`/`prod`, default `prod`) and a lifespan guard that refuses any development-only
   flag with `ENV=prod`. The registry is derived from the fields declared with
   `dev_only_flag(...)` (none until Phases 13/16/19/20), so there is no list to forget; a
   boolean field *named* like a dev feature (`fake_`, `_lab`, `explorer`, `code_fix`, `debug`,
   …) counts as dev-only even if undeclared, and a unit test fails until it is declared.
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
