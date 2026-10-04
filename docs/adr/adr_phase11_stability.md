# ADR phase 11 — Stability: data persistence and rerank latency

- **Status:** 11a **Accepted** locally (2026-10-01: every 11a decision below has landed on
  the phase branch, `gate.sh --full` PASS); Azure evidence (promotion, smoke, cost) is added
  at the 11a promotion; 11b is still open.
- **Parts:** **11a** — persistence, migrations Job, asynchronous erasure (branch
  `phase-11a-persistence`); **11b** — rerank latency and baked model images (branch
  `phase-11b-latency`).
- **Related:** [ADR phase 6](adr_phase06_gdpr_erasure.md) (erasure, refined by 11a),
  [ADR phase 9](adr_phase09_deployment.md) (migrations at container start, superseded by
  11a), [ADR phase 10](adr_phase10_azure.md) (Azure deployment).
- **Phase report:** [Phase 11a report](../phases/phase-11a.md) — Azure promotion, smoke,
  restore rehearsal and cost evidence.

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

Classes (PHASE_PLANNING 11.1): **(a)** data gone, **(b)** unreadable because the master key
changed, **(c)** session only (the data is there and readable; only the login/JWT broke).
Evidence is counts only — no row contents, emails, hashes or keys.

### Local (11.1, T11.1.1) — 2026-09-29

**Method (the development volumes were never mounted by a database).** Both dev DB containers
were already stopped (`rag_ia-db-1` exited 2026-09-24, `rag_app-db-1` exited 2026-09-27), so
the files did not change during the copy. Both had exited with code 255 (an unclean stop), so
the copies are **crash-consistent**, not cleanly shut down: the restore below replayed the
write-ahead log on its first start, exactly as after a power cut. Each volume was copied
read-only into a mode-700 directory outside the repository (`~/secrag-backups`), the copy
restored into a new throw-away volume, and a
throw-away `pgvector/pgvector:pg16` (the image the dev compose uses; data directory
`PG_VERSION` 16) started on it on a loopback port:

```bash
docker run --rm --network none -v rag_ia_pgdata:/src:ro -v ~/secrag-backups:/dst alpine \
  tar -C /src -czf /dst/rag_ia_pgdata-20260929T202333Z.tgz .          # same for rag_app_pgdata
docker volume create secrag-diag-rag_ia_pgdata
docker run --rm --network none -v secrag-diag-rag_ia_pgdata:/dst -v ~/secrag-backups:/src:ro alpine \
  tar -C /dst -xzf /src/rag_ia_pgdata-20260929T202333Z.tgz
docker run -d --name secrag-diag-rag_ia_pgdata-db -p 127.0.0.1:15441:5432 \
  -v secrag-diag-rag_ia_pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16   # rag_app → 15442
psql -h 127.0.0.1 -p 15441 -U rag -d rag -X -At -F'|' -f scripts/azure/sql/diag_counts.sql
```

The snapshots (sha256 `96989e30…eafcf37` rag_ia, `34db794e…e836291a` rag_app) are kept as a
backup; the throw-away containers and volumes were removed afterwards. Before and after, the
dev volumes' `CreatedAt` were unchanged (`rag_ia_pgdata` 2026-09-19T02:27:17Z,
`rag_app_pgdata` 2026-09-20T14:34:58Z) and no new container referenced them.

**Volumes.** `docker-compose.yml` declares `pgdata` without `name:`, so Docker prefixes it with
the compose project, i.e. the checkout folder: two volumes exist, each labelled with its
project (`com.docker.compose.project` = `rag_ia` / `rag_app`).

| Item (counts) | `rag_ia_pgdata` | `rag_app_pgdata` |
|---|---:|---:|
| PostgreSQL / `alembic_version` | 16.15 / `0004_conv_titles_tokens` | 16.15 / `0004_conv_titles_tokens` |
| `users` / `user_keys` / users without a key | 5 / 5 / 0 | 0 / 0 / 0 |
| `conversations` / `messages` | 4 / 13 | 0 / 0 |
| `deletion_requests` / `email_verification_tokens` | 4 / 9 | 0 / 0 |
| `documents` / `chunks` (corpus) | 9 / 322 | 9 / 322 |
| users / messages created after `rag_app_pgdata` appeared | 2 / 10 | 0 / 0 |

**Key check** (the T11.1.3 snippet `scripts/azure/diag_keys.py`, run with the local
`backend/.env` settings against each copy): `rag_ia` copy — N 5, **OK 4, KO 1**, `JWT_SECRET`
length ≥ 32 OK, `DATA_MASTER_KEY` valid Fernet OK; `rag_app` copy — N 0.

**Local classification.**
1. **Not (a):** nothing was deleted. Every account and conversation is in `rag_ia_pgdata`;
   `rag_app_pgdata` holds the schema and a re-ingested corpus only (no user rows). The "loss"
   is a **volume split**: whenever the stack ran as project `rag_app` it used the other,
   user-less database. Both projects were used after 2026-09-20 (2 users and 10 messages were
   written to `rag_ia_pgdata` after `rag_app_pgdata` appeared), so the data seemed to come and
   go with the folder the stack was started from.
2. **Also (b) for one account:** 1 of 5 wrapped keys does not unwrap with today's local
   `DATA_MASTER_KEY`, so that account's content is unreadable locally. The counts cannot show
   which key wrapped it. Likely cause: two key sources in local development — the compose
   backend reads `DATA_MASTER_KEY` from the shell or a root `.env`, native runs read
   `backend/.env` — or a regenerated key. The fingerprint check (T11.2.4) turns this into a
   refusal at start-up instead of silent `InvalidToken`s.
3. `rag_app_pgdata` is **not empty** (planned: "empty"): it holds the corpus, which can be
   re-ingested, and no personal data. T11.2.1 may remove it after the user confirms. The
   snapshot is kept.

### Tools for the Azure diagnosis (T11.1.2, T11.1.3)

- **`scripts/azure/db-tunnel.sh`** — temporary access to the Flexible Server. Every call first
  sweeps stale `secrag-tunnel-*` firewall rules. It detects the public IPv4 from two services,
  which must agree and be public (fail closed), creates one `/32` rule, and runs `psql` or
  `pg_dump` only. The connection is `sslmode=require` and read-only by default
  (`default_transaction_read_only=on`; `--read-write` is explicit). The read-only default is a
  **guard against accidents, not a security control**: it is a session default that `SET` or
  `BEGIN READ WRITE` override, and with `--password-from-app` the session runs as the app's
  role (today the database owner). Once row 40's least-privilege roles exist, read-only
  sessions use a role that has only `SELECT`. The password comes from
  the backend's `database-url` secret into a 0600 `PGPASSFILE`; it never reaches argv or the
  output. The rule is **always** removed (EXIT trap, including SIGINT/SIGTERM/SIGHUP, with
  signals ignored during the removal), and then verified gone; otherwise the script prints the
  manual command and fails. Tested against a stateful fake `az`/`curl`/`psql`
  (`backend/tests/test_db_tunnel.py`, 22 cases: rule present only during the call; removed on
  success, error, SIGINT and SIGTERM; sweep; `--sweep`; seven unknown-IP cases; read-only;
  password never printed; the delete-failure path). Mutations (INT trap, sweep, read-only
  removed) fail the tests. *Hardened in 11.2 (DA-D-4, DA-D-5):* the command runs as a
  waited-for child (default SIGINT and the caller's stdin restored), so a SIGINT/SIGTERM/SIGHUP
  sent only to the script's PID is forwarded at once and the command gets 5 s before
  SIGTERM/SIGKILL — the rule never outlives a signal, even for an interactive `psql`; a signal
  during `create` makes the cleanup watch 60 s for a late rule; a URI or password anywhere in
  an argument (`--dbname=postgresql://…`, `-d "… password=…"`) is refused. Tests cover SIGHUP,
  signals to the script's PID only, a `psql` that ignores SIGINT and the late rule; the
  foreground-child, no-watch and prefix-only-guard mutations fail them.
- **`scripts/azure/sql/diag_counts.sql`** — the counts above, as `name|value` lines.
- **`scripts/azure/diag_keys.py`** — runs inside the backend and reads settings and the DB as
  the app does, in a read-only transaction. It prints exactly five lines (`N`, `OK`, `KO`,
  `JWT_SECRET length >= 32: OK|KO`, `DATA_MASTER_KEY valid Fernet: OK|KO`); a failure prints
  the exception class only. `scripts/azure/exec_oneliner.py` wraps it into one
  whitespace-free `python -c …` argument for `az containerapp exec --command`. It was verified
  in the backend image `rag_app-backend:bc7c661` (same Dockerfile as the deployed `dee9cbc`)
  in four ways: as argv, through `sh -c` (`--for shell`), over stdin (`python -`), and with a
  wrong key (KO = N).

### Azure (T11.1.3) — 2026-09-29/30 (run by the orchestrator, read-only)

| Item | Result |
|---|---|
| Spike: stdin (`python -`) through `exec` / `--command` split on whitespace or by a shell | **stdin is not possible** with the Linux `az` (`containerapp exec` needs a TTY: `termios.error` 25). **Split mode works** with the Windows CLI: `--command "python -c print(12345)"` printed `12345` (split on whitespace, no shell). The snippet ran as one whitespace-free argument built by `exec_oneliner.py` (1733 characters) |
| db-tunnel counts: `users` / `user_keys` / users without a key / `conversations` / `messages` / `alembic_version` | 2 / 2 / 0 / 9 / 17 / `0004_conv_titles_tokens` (PostgreSQL 16.15; also `deletion_requests` 0, `email_verification_tokens` 9, `documents` 9, `chunks` 322) |
| Key snippet: N / OK / KO | **2 / 1 / 1** |
| `JWT_SECRET` length ≥ 32 / `DATA_MASTER_KEY` valid Fernet | OK / OK |
| Firewall: no `secrag-tunnel-*` rule left after the runs | Yes: the tunnel's rule was created and removed on every call, including a SIGINT drill during `psql` (rc 130, removal confirmed). The standing single-IP rule `AllowMyIP` (stale, not the current IP) was **deleted on 2026-09-30**; only `AllowAllAzureServicesAndResourcesWithinAzureIps_…` remains (needed by Container Apps without a VNet; accepted risk, D-2026-09-29-3) |
| **Classification** (a / b / c) and consequence (R5-5 in row 40 if b) | **(b) unreadable — key changed, for 1 of 2 accounts** (the older one; the fresh test account unwraps). Nothing is gone (not (a)). Consequence: before the first fingerprint write on Azure (row 40), a time-boxed key recovery with candidate keys (D-2026-09-29-2 (b)), then R5-5 — purge with a tombstone — for whatever still fails |

### In-memory state (T11.1.4)

Everything below lives in the API process. It is lost on every restart (and on every
scale-to-zero on Azure), and with more than one replica each replica has its own copy.

| State | Where | Lost on restart | Diverges across replicas | Fix |
|---|---|---|---|---|
| Rate-limit token buckets (`chat:<ip>`, `login:<ip>`) | `api/deps.py` `_rate_limiter` (`ratelimit.RateLimiter`) | yes: a restart refills every bucket (the login brute-force budget resets) | yes: each replica allows the full budget | Phase 16.3 (shared store, Valkey) |
| Signup velocity per IP (1 h window, risk score) | `api/deps.py` `_signup_tracker` (`risk.SignupTracker`) | yes: the anti-Sybil window restarts empty | yes | 16.3 |
| Engine and session factories (connection pools) | `api/deps.py` `_session_factory`, `api/conversations.py` `_stream_sessions` | rebuilt (no user state) | one pool per replica: DB connections × replicas | none; watch `max_connections` from 16.3 |
| Reranker model (`CrossEncoder`, ~2.2 GB) | built **per question** (`generation.py`); files in the container's Hugging Face cache (ephemeral disk) | re-downloaded after a restart/new replica | per replica | 11b (one shared instance, baked into the image) |
| `bge-m3` in `secrag-ollama` (Azure) | Ollama's ephemeral storage | yes: scale-to-zero loses it (F-2026-09-27-5) | per replica | T11.4.3 (baked image) |
| Login sessions (JWT) | stateless tokens signed with `JWT_SECRET` | survive restarts **only if `JWT_SECRET` is unchanged**; a new secret logs everyone out (class c) | no (same secret) | fail-fast length check T11.2.2; server-side sessions 12a |
| Settings, password hasher | `config.get_settings()` (read on every call), `security._hasher` | no state | no | — |

User data (users, keys, conversations, messages, verification tokens, deletion requests) is
only in PostgreSQL.

### Red restart check (T11.1.5)

`scripts/restart_check.sh` does: register a throw-away account → one chat turn → `compose down`
(volumes kept) → `up` → login → decrypted read. It refuses the development projects, any
project that resolves to a development volume, and any project whose `pgdata` already exists;
it removes both projects (`down -v --rmi local`) at the end. It uses throw-away projects in
place of `-p rag_ia` / `-p rag_app`, with the same mechanism (2026-09-29):

- `--project secrag-rc-same` (same project before and after): **PASS** (41 s).
- `--project secrag-rc-ia --restart-project secrag-rc-app`: **FAIL (rc 1)** at "login after
  restart" — `HTTP 401 … the restarted stack runs on volume secrag-rc-app_pgdata, the data
  was written to secrag-rc-ia_pgdata`. This is the diagnosed cause. It turns green once the
  dev volume has a fixed name (T11.2.1); T11.2.8 makes it the gate's `restart-check` step.

**Green (T11.2.1 + T11.2.8, 2026-09-30).** The same cross-project run
(`--project secrag-rc-ia --restart-project secrag-rc-app`) re-run at `f5f15e2` (compose before
T11.2.1) still FAILS with the 401 above; with the fixed external volume it **PASSES** (57 s,
including the master-key variant below). The final check runs the real `docker-compose.yml`
plus `scripts/restart_check.compose.yml` (a throwaway external volume, API on
`127.0.0.1:18000`, no DB port) and also proves that the backend image alone never migrates,
that the `migrate` one-shot exits 0, and — `--restart-master-key` (DA-D-2) — that a restart
with a new random `DATA_MASTER_KEY` is **refused** ("does not match the master-key
fingerprint"). With the fingerprint check mutated away, that step FAILS with
`login 200, read 500` — exactly the class (b) symptom of 11.1.

## Decisions

Decisions 1–7, 9 and 10 landed in 11a (the italic notes say where); decision 8 is 11b.

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
   *Landed in 11.2 (T11.2.1):* `docker-compose.yml` declares `pgdata` as the **external**
   volume `rag_ia_pgdata` — the one that held the data (the 11.1 diagnosis) — so its name no
   longer depends on the compose project, and compose never creates, recreates or removes it
   (`down -v` included). `scripts/dev/create_dev_volume.sh` creates it once on a new machine
   and never touches an existing one. The gate never references it: `compose.gate.yml` has
   its own volume, and `restart_check.sh` swaps it for a throwaway volume and refuses any
   development volume. The stray `rag_app_pgdata` (schema + corpus only, no user rows) was
   removed by hand after read-only checks (D-2026-09-30-1); it no longer exists (checked
   2026-10-01).
2. Fail-fast settings validation in the API lifespan; master-key fingerprint. *Landed in 11.0:*
   `ENV` (`dev`/`prod`, default `prod`) and a lifespan guard that refuses any development-only
   flag with `ENV=prod`. The registry is derived from the fields declared with
   `dev_only_flag(...)` (none until Phases 13/16/19/20), so there is no list to forget; a
   boolean field *named* like a dev feature (`fake_`, `_lab`, `explorer`, `code_fix`, `debug`,
   …) counts as dev-only even if undeclared, and a unit test fails until it is declared.
   *Landed in 11.2 (T11.2.2, T11.2.4):* the API lifespan (never import time, never a Job)
   runs, in order: `validate_api_settings` — `DATABASE_URL` is a PostgreSQL URL and, with
   `ENV=prod`, explicitly set; `JWT_SECRET` ≥ 32 characters; `DATA_MASTER_KEY` a valid Fernet
   key; `ENV` — reporting every problem by name, never a value; the dev-flag guard; and the
   **master-key fingerprint** (`rag_app/keycheck.py`): the one-row table
   `master_key_fingerprint` (0005) holds `HMAC-SHA256(key, context)`; a mismatch refuses to
   start. The **first** write is guarded by R5-5: while any stored user key does not unwrap
   with the running key, the API refuses to start and stores nothing, so a fingerprint can
   never bless a key that cannot read existing data (the first start after 0005 on the
   developer's DB and on Azure therefore happens only after the key recovery below). Jobs
   use `JobSettings` (`ENV`, `DATABASE_URL` only): Alembic's `env.py` and `make_engine` read
   it, so a Job starts without `JWT_SECRET`/`DATA_MASTER_KEY`.
3. Migrations out of the container command: compose one-shot service and an Azure
   migration Job run by CD before the apps; expand/contract rule. *Landed in 11.2
   (T11.2.3, T11.2.5–7, T11.2.9, T11.2.11):* the backend `CMD` is uvicorn only; compose runs
   the one-shot `migrate` (jobs image) after `db-roles`, and the backend waits for it. CD's
   order is images (backend, frontend, jobs) → `migrate` job (re-checks `secrag/gate-full`;
   `scripts/cd/azure_jobs.sh`: `job update --image <jobs digest>` verified, `job start` with
   no overrides, wait; Failed/Stopped/Degraded or a timeout stop the pipeline) → apps → purge
   and backup Job images (digest). Job names come from repo variables; the Jobs are
   versioned YAML (`deploy/azure/jobs/*.yaml`: placeholder image and secrets, parallelism 1,
   timeout, retry limit; the migration Job has a Manual trigger, the purge and backup Jobs a
   **Schedule trigger with the dormant cron `0 0 1 1 *`** — D-2026-10-01-5, because
   `az containerapp job update` cannot change a trigger type, only the cron of a Schedule
   Job; the real cron is set by hand after the promotion's CD run) applied by hand — CD never
   applies YAML or touches schedules. A dry run prints the same order step by step, and the dispatch input
   `simulate_failure` (dry runs only) stops it at the migration step. **Migration 0005**
   (expand): `master_key_fingerprint`, `users.deleted_at`, nullable `email`/`password_hash`,
   tombstone status/progress/attempts/last error (old tombstones = `done`), `purger_runs`,
   `usage_daily`, and the migration map's grants (`secrag_purger` may lock by `UPDATE (id)`
   and delete leaf rows but never write content or touch the corpus; `secrag_backup` reads
   everything); it fails clearly without its roles, and its downgrade refuses while erased
   users exist. It is authored once and extended in place by 11.2b before any shared apply
   (TF7). The expand/contract rule is in the
   [Definition of Done](../DEFINITION_OF_DONE.md#phase-specific-norms); `migrations-roundtrip`
   runs locally and in CI, and a broken downgrade fails it.
4. Slim `jobs` image for the migration, purge and backup Jobs. *Landed in 11.2 (T11.2.12):*
   `backend/Dockerfile.jobs` — `python:3.12-slim` pinned by digest, PGDG
   `postgresql-client-16` (signing key checked by fingerprint), `age`, `rag_app` without
   torch (`requirements-jobs.txt`, resolved with `requirements.txt` as constraints), Azure
   Blob client, non-root (uid 10001), ≈ 490 MB. CI job `jobs-image`: `import rag_app.erasure`
   works, `import torch` fails, `pg_dump` 16 + `age` present, jobs pins audited.
5. Encrypted backups (14-day retention) with tombstones exported outside the database.
   *Landed in 11.2 (T11.2.10, T11.2.13):* `scripts/db/backup.sh` dumps **as
   `secrag_backup`** (it refuses any other role) and pipes `pg_dump -Fc` straight into `age`
   with the **public** key only — no plaintext dump ever touches a disk. **File mode** writes
   `secrag-<UTC timestamp>.dump.age` (0600) into a 0700 folder outside any git work tree and
   deletes dumps whose *name* time is older than the retention (a copied or touched file keeps
   its real age). **Blob mode** (the backup Job, jobs image, managed identity) encrypts to a
   private temporary file, then uploads it to a new blob `backups/secrag-<ts>.dump.age` (never
   overwrites; refuses input without the `age` header) and then runs the **oldest-blob
   check**: the Job fails when there is no backup blob or the oldest one is older than the
   promise (the Storage lifecycle rule deletes `backups/` after 12 days, so an older blob
   means the rule broke). **A failed dump leaves nothing** (DA-F-2): a failed or truncated
   `pg_dump` still produces a valid `age` stream, so both modes check the exit codes of
   `pg_dump` **and** `age` and promote the temporary file (rename / upload) only when both
   are 0; otherwise it is deleted and the run fails. **Retention** is one constant, X9 =
   **14 days**, in `backend/src/rag_app/retention.py`; the scripts read it from that file,
   and a test checks the scripts and these docs against it. The pruners only ever delete
   files whose name matches the dump (or tombstone export) pattern; a matching name with an
   invalid date or a time more than a day in the future is kept but **reported** and the run
   fails (it would otherwise be kept forever), and a leftover `.<name>.partial` older than a
   day (a killed run) is removed (DA-F-3). **A dump is readable only with the same
   `DATA_MASTER_KEY`** (DA-F-7): it holds the wrapped user keys and the stored master-key
   fingerprint, so the master key is escrowed next to the age private key (password manager
   + offline copy), and the app that serves a restored database must use that very key (any
   other key fails closed at start-up). `scripts/db/restore.sh` runs on
   the owner's machine (the only place the private key exists — password manager + an offline
   copy, R4-1): it refuses a non-private identity file or one inside a git work tree, invalid
   tombstone exports, and a target database that is not **empty**; it decrypts in a stream into
   `pg_restore --single-transaction --exit-on-error` as the owner (the source owner's
   default-privilege entries are skipped and `db/roles.sql` re-applies them), runs `alembic
   upgrade head`, then applies the **union** of the restored and the exported tombstones and
   `replay_deletions` before the app reopens. **Tombstone export format**
   (`rag_app/tombstones.py`; the purger of 11.2b writes it every run to Blob `tombstones/`,
   locally the git-ignored `.tombstones/`): `tombstones-<ts>.jsonl`, one
   `{"user_id", "requested_at"}` object per line, opaque ids only; any malformed line refuses
   the whole restore. `scripts/db/backup-pull.sh` (run weekly by the owner with the Linux `az`,
   `--auth-mode login`) copies the newest dump and the newest tombstone export, still
   encrypted, and prunes local copies with the same constant. The backup Job image gets
   `backup.sh` through a named build context (`dbscripts=scripts/db`). **Gate step
   `backup-drill`** (throwaway databases on the gate server, throwaway age keys):
   backup → erase a test user → restore with the **offline copy** of the key (the working
   copy is shredded first) → the user stays erased (tombstone `done`, no key, conversation or
   message) and the other user is intact; a 15-day-old dump is removed, a 13-day-old one and
   an unrelated file are kept; controls: without the tombstone export the erased user **comes
   back** (so the export is what keeps them erased), a wrong key restores nothing, a non-empty
   target is refused. The real Blob run and `backup-pull.sh` against Azure are row 42.
6. Asynchronous erasure: short request transaction (crypto-shred + PII scrub) → 202, then a
   batched, resumable purger. *Landed in 11.2b (T11.2b.1–8):* **refines ADR phase 6**
   ("immediate hard delete" → "immediate crypto-shred + PII scrub, batched physical deletion
   ≤ 24 h, backups ≤ 14 days"; see its *Refinement* section). `DELETE /account` →
   `rag_app.erasure.request_erasure`: one transaction with `SET LOCAL lock_timeout = '2s'`
   and `statement_timeout = '5s'` deletes the `user_keys` row, sets `email`/`password_hash`
   to NULL and `deleted_at`, and upserts the tombstone `pending` → **202** with
   `ERASURE_ACCEPTED_MESSAGE` (the 24 h deadline and the X9 constant, tested); no export in the
   request path. `get_current_user` and login ignore `deleted_at` users (a live token → 401
   at once). **Purger** (`rag_app.purger`; CLI `python -m rag_app.erasure purge |
   purge-now <request-id> | loop`), run as `secrag_purger` (0005 grants — no schema change
   was needed, 0005 stays as authored): `pg_try_advisory_lock` on its own connection (an
   overlapping run is skipped); a `purger_runs` row; for each open tombstone (`pending`,
   `running`, `failed`) it claims it (`running`, attempts + 1), refuses an account without
   `deleted_at`, and follows the registry `PURGE_STEPS` leaf-first — each batch (1,000 rows)
   is its own transaction (`lock_timeout` 2 s, `statement_timeout` 5 s, `FOR UPDATE SKIP
   LOCKED`) that also records the per-step progress; a timeout or rows held by another
   transaction back off exponentially (0.5 → 8 s) and after 5 retries the tombstone is
   `failed` (error class only; retried next run). A killed run leaves `running` + progress
   and the next run resumes; `--max-seconds` (1500) stops claiming work before the Job's
   replica timeout. *G1 fixes:* every run (a skipped one too) counts the open tombstones and
   those open for more than 24 h — any **overdue** one prints `purger: WARNING: N erasure(s)
   overdue` and exits 1, so a Job that never runs on schedule, keeps failing or keeps being
   skipped fails visibly (DA-G1-1); `--until-done` (used by `restore.sh`) exits 1 while any
   tombstone is still open, so a restore whose purger pass stops at its time limit never
   prints "DONE" (DA-G1-8). A lock/statement timeout in the request path answers **503** with
   `Retry-After` (nothing changed) instead of a 500 (DA-G1-4); `/auth/verify` ignores erased
   accounts (DA-G1-5); `backup-pull.sh` picks the newest dump/export among valid,
   non-future names only (DA-G1-3). **X3 scan:** `uncovered_user_columns` scans `information_schema` for
   `user_id`, `*_user_id`, `admin_id`, `*_hmac`; exemptions `deletion_requests.user_id`,
   `user_keys.user_id`. **Tombstone export** on every run: Blob `tombstones/` when
   `TOMBSTONE_STORAGE_ACCOUNT` is set (managed identity), else a local folder (compose:
   `./.tombstones`); entries `done` for more than 15 days are left out; **exports older than
   30 days are pruned, the newest valid one always kept** (DA-F-8, user decision
   2026-09-30), and while any export name cannot be judged (invalid or future date) nothing
   is pruned and the run exits 1 (DA-F2-1 — the same rule now in `backup_lib.sh
   prune_by_name … keep-newest`, used by `backup-pull.sh` with the 30 days).
   `replay_deletions` re-applies the short step and re-queues; `purge_orphaned` enqueues;
   `restore.sh` runs the purger (owner, `--no-export`) after the replay; `backup-drill` now
   erases through the request path and purges as `secrag_purger`. **Local runner:** compose
   service `purger` (jobs image, `loop --interval ${PURGE_INTERVAL_SECONDS:-3600}`, runs as
   the WSL user so the exports are theirs). **Gate step `erasure-scale`**
   (`rag_app.devtools.erasure_scale`, throwaway `secrag_scale_*` database): three synthetic
   users with 100,000 messages each, the real API in a child process (pool 5 + 10), 8
   workers of sessions (a login, then 6 listings and a read; a few stub chat calls — no GPU);
   each synthetic user erases itself during the load. Criteria: the **median** erasure
   request < 200 ms and every request's work outside its COMMIT's WAL flush < 200 ms — on
   this laptop a single commit on the Docker Desktop volume occasionally waits 0.3-0.9 s for
   the disk flush (seen as `COMMIT 862 ms` with every statement < 10 ms and no other WAL
   written; an idle probe had 0 of 800 commits over 12 ms), so one sample alone made the
   gate flaky for a reason unrelated to erasure; every sample and its COMMIT time are
   printed. The seed is settled (VACUUM ANALYZE + CHECKPOINT) before the measured window.
   Measured on 2026-09-30 (6 runs, 18 requests): erasure request 35-74 ms (median per run
   39-60 ms; the transaction 17-32 ms), ~2,400 load requests per run with 0 failures, 0 lock
   errors, 0 pool timeouts, pool peak **9 of 15**; the purger killed at 16,000 messages
   (tombstone `running`), an overlapping run skipped, the next run purged the 300,000
   messages in 2.2-3.3 s (tombstones `done`, 0 rows left, longest transaction ≤ 0.044 s).
7. Global daily answer cap (R6-1). *Landed in 11.2 (T11.2.14):* registration is open and
   every cloud request arrives through one shared proxy IP, so the per-IP limiter cannot
   bound total spend and the budget alert only notifies. `rag_app.usage_cap` counts
   **answers per UTC day for all users together** in `usage_daily` (0005: `day`, `answers`,
   `tokens` — no personal data). The answer is **reserved at request start**, before any LLM
   call, with one race-safe statement (`INSERT … ON CONFLICT (day) DO UPDATE SET answers =
   answers + 1 WHERE answers < cap RETURNING`; 8 concurrent requests at `cap − 1` get
   exactly one answer). A turn that fails *before* the pipeline (the data key does not
   unwrap — checked before the reservation — or its user message cannot be stored, which
   gives the reservation back) does not count; a turn that fails or is interrupted *in* the
   pipeline does, because it may have spent tokens; the canned greeting makes no LLM call and
   is not counted. At the cap: `/chat` → **429** with `Retry-After` (seconds to UTC
   midnight); `/chat/stream` → the `conversation` event and an `error` event
   `daily_cap_reached` with a friendly message, nothing stored, no pipeline. The counter
   failing closes the door (`storage_failed`), it never opens it. `DAILY_ANSWER_CAP` defaults
   to **150** in code — the Azure value (D-2026-10-01-1), so Azure is capped at it even if
   the variable is lost (gotcha 7) — `0` switches it off with `ENV=dev` only; `ENV=prod`
   refuses 0 and negative values. Every LLM call passes `max_tokens` (generation 1024,
   groundedness 16), so one answer has a bounded worst-case cost — the table under
   [Costs](#costs) is the basis of the 150 choice.
8. (11b) One shared reranker, baked model images, latency gate.
   *Landed in 11.3 (T11.3.1–T11.3.3, block A):* **per-stage timing** — classify, query embed,
   hybrid search, rerank (load vs inference), generate, groundedness, time to first token,
   total — one JSON line per answer on both `/chat` and `/chat/stream` (`rag_app.timing`,
   stdout; never the question, the answer, a user id or an IP). **Prometheus** — histograms
   labelled only by `stage` (`secrag_chat_stage_seconds`) plus an in-flight chat requests
   gauge (`secrag_chat_requests_in_flight`, no labels), served on a **separate internal port**
   (`METRICS_PORT`, default 9100; TF4) — the FastAPI app registers no `/metrics` route at all,
   so the API port never serves it. A `prometheus` container (`docker-compose.yml`, image
   pinned by digest per X1) scrapes that port only, 7-day retention, UI on `127.0.0.1:9090`.
   The reranker still reloads per request here (`rerank_load` dominates the measurements
   below) — T11.4.1 fixes that. End-to-end proof (throwaway compose project, native Ollama,
   322 indexed chunks, torn down after): one `/chat` call —
   `{"embed_ms": 293.0, "hybrid_search_ms": 5.1, "rerank_load_ms": 32079.1,
   "rerank_inference_ms": 9378.6, "generate_ms": 2943.2, "groundedness_ms": 147.8,
   "ttft_ms": 44702.3, "total_ms": 44850.3}`; one `/chat/stream` call —
   `{"classify_ms": 0.0, "embed_ms": 1327.3, "rerank_load_ms": 1071.9,
   "rerank_inference_ms": 8781.1, "generate_ms": 1878.4, "groundedness_ms": 591.1,
   "ttft_ms": 11978.5, "total_ms": 13657.0}`; `secrag_chat_stage_seconds_count` visible in
   Prometheus for every stage above; `GET /metrics` on the API port → 404.
   *Landed in 11.3 (T11.3.4–T11.3.5, block B):* **explicit `num_ctx` per call type**
   (`Settings.num_ctx_answer` / `num_ctx_groundedness`, `llm.py`/`generation.py`) — Ollama's
   built-in default (2048) is already close to or below the worst case today (rerank_top_n=4
   chunks of up to chunk_size+chunk_overlap ≈ 1350 chars each + the system prompt + the
   question + the output budget), so a request could be silently truncated with no error; a
   request whose estimated size (chars/4, a deliberately crude heuristic — no tokenizer
   dependency added just to warn) would not fit its `num_ctx` now logs a warning
   (`rag_app.llm`, reaches stderr via `logging.lastResort` without needing `rag_app.timing`'s
   own-handler workaround, since `.warning()` is at/above its threshold). **Critical measured
   finding: both settings MUST be equal.** Probing the Ollama API directly (same model,
   `num_predict` varied — no reload; `num_ctx` varied — full reload every time, confirmed
   with `load_duration` in the response): switching `num_ctx` on an already-loaded qwen costs
   **~6.3 s**, and that includes a request that *omits* `num_ctx` entirely (Ollama then uses
   its own default, 2048, which already differs from either setting) — so generate and
   groundedness must load the model at the SAME `num_ctx`, else one answer pays the reload
   twice (once for groundedness, once more for the next answer's generate). Guarded by
   `test_num_ctx_answer_and_groundedness_must_match`. *Fixed in 11.4 (DA-11bB-1, block C):*
   block B's own wording ("CLI/eval only — not on the live API path today") was already
   false for the eval judge — `eval/benchmark.py`/`eval/runner.py` reuse ONE `OllamaChat`
   across generate -> groundedness -> `judge_correctness` (and the agentic router's query
   rewrite, when exercised) on every golden-set item, i.e. the SAME process/keep-alive window
   as generate/groundedness, inside the `eval` gate step itself. Both `agentic.reformulate`
   and `eval.judge.judge_correctness` now pin `num_ctx_answer` too, so neither forces a reload
   against the resident qwen; `test_router_and_judge_calls_now_pin_num_ctx_to_match_answer_
   groundedness` (renamed from the block B version, which asserted the opposite) guards it.
   **VRAM measured** on the development machine (RTX 4060, 8 GiB; native
   `ollama serve`; `bge-m3` warmed first, baseline 742 MiB already in use by the desktop/
   Xwayland): qwen2.5:7b-instruct-q4_K_M alone added ≈4.53 GiB at `num_ctx`=2048, ≈4.64 GiB
   at 4096, ≈4.87 GiB at 8192 (`ollama ps`: 5.0 GB resident at 8192); `bge-m3` added a further
   ≈0.74 GiB, 664 MB resident. Total with both models resident at `num_ctx`=8192: **6.35 GiB
   of 8 GiB** (≈1.8 GiB / 22 % headroom) — chosen value for both settings. The cross-encoder
   reranker never touches VRAM at all in this repo: `requirements-torch.txt` pins a **CPU-
   only** torch build (`torch==2.14.0+cpu`, confirmed `torch.cuda.is_available() is False` in
   the backend venv), so "reranker on GPU" is not possible today regardless of `num_ctx`
   headroom — T11.5.1's device axis (CPU vs GPU) needs a GPU torch variant added first.
   **"Before" latency baseline** (`rag_app.eval.latency`, golden set — 14 questions — × N=3
   runs = 42 answers, reranker forced to CPU via `CUDA_VISIBLE_DEVICES=""` before any model
   loads, same per-call-site shape `generation.answer_question` uses incl. today's fresh-
   reranker-per-call reload; `eval/latency_baseline.json`, git commit `b77f69f`):
   `rerank_load` p50 1063 / p95 1122 ms (n=42), `rerank_inference` p50 7913 / p95 9204 ms
   (n=42, CPU cross-encoder inference over the `top_k`=20 candidate pool dominates the
   pipeline far more than the reload itself), `generate` p50 883 / p95 2820 ms (n=42),
   `groundedness` p50 151 / p95 560 ms (n=30 — 12 of 42 answers correctly abstained before
   reaching it), `embed` p50 15 / p95 40 ms, `hybrid_search` p50 3 / p95 5 ms, **`ttft` p50
   9938 / p95 11373 ms, `total` p50 10212 / p95 11812 ms**. Runtime 429 s (≈7.1 min) for the
   42 answers — recorded by the gate step `latency` (`--full`/`--only latency`, records only
   in this block; `T11.6b.1` adds the pass/fail floor once T11.4.1/T11.5 pick a new
   baseline). `rerank_inference` being far larger than `rerank_load` here is a new finding
   beyond block A's single-call ADR evidence above (which only showed one sample each): T11.4
   (shared reranker, eliminates the ~1.1 s reload) and T11.5 (CPU-friendlier reranker/top_k)
   both matter — the reload is real but not the biggest cost at this `top_k`.
   *Landed in 11.4 (T11.4.1–T11.4.2, block C):* **DA-11bB-1 fixed first** (eval judge/agentic
   rewrite reload risk, see point 7 above) — `eval.judge.judge_correctness` and
   `agentic.reformulate` now pin `num_ctx_answer` too, since `eval.benchmark`/`eval.runner`
   already share ONE `OllamaChat` across generate -> groundedness -> judge (and the router's
   rewrite) in the same keep-alive window, inside the `eval` gate step itself, not a future
   risk. **Single shared reranker (T11.4.1):** `reranking.get_shared_reranker()` is the ONE
   process-wide instance (double-checked locking), loaded and warmed up (one dummy `predict`)
   in the FastAPI lifespan before the app serves traffic; both `/chat`/`/chat/stream` (via
   `api/deps.py`/`api/conversations.py`), the `eval` gate step (`eval.benchmark`/
   `eval.runner`) and `agentic.answer_agentic` default to it instead of constructing their own
   — proven by a constructor call count (`test_get_shared_reranker_constructs_the_model_
   only_once_across_many_calls`, `test_chat_reuses_the_shared_reranker_across_requests`: 5 and
   3 calls respectively, ONE construction each). `max_length=512` pins the cross-encoder's
   per-pair token cap (`RERANKER_MAX_LENGTH`, `reranking.py`). **Informal before/after**
   (own micro-benchmark, same backend venv, native Ollama, 20 candidate chunks, `top_n`=4; the
   formal T11.4.4 "after" baseline is block E's job): 5 back-to-back `rerank()` calls on the
   shared instance — call 1 pays `rerank_load` **3623 ms** (cold load this run; order-of-
   magnitude consistent with the 1063–1086 ms baseline measured on a warmer disk cache) +
   `rerank_inference` 554 ms; calls 2–5 have **NO `rerank_load` stage at all** (`_ensure_model`
   short-circuits), only `rerank_inference` ≈485–505 ms each — i.e. every request after the
   first pays **zero** reload cost instead of the ~1.1 s baseline, every time. The committed
   `eval/latency_baseline.json` is intentionally **untouched** by this block (it is the
   "before" reference); a routine (non-`--update-baseline`) `latency` gate run on this block's
   code (`eval/latency_results.json`, git-ignored) shows `rerank_load` p50 **1085.7 ms**
   essentially unchanged from baseline — **by design, not a regression**: `eval.latency`'s own
   benchmark loop still constructs a FRESH `CrossEncoderReranker()` per golden-set item on
   purpose (its own docstring), so it does not exercise T11.4.1 at all; it inlines
   `reranking.retrieve`/`generation.answer_from_chunks` directly rather than calling
   `generation.answer_question` (which now defaults to the shared instance). T11.4.4 (block E)
   must decide how the "after" measurement accounts for this — switching `eval.latency` itself
   to the shared instance, or adding a second run that does, before the `rerank_inference` ≤
   50 %/≤ 1.5 s floor can be checked honestly against a methodology that actually reflects
   production. **Reranker baked into the backend image (T11.4.2):** `backend/Dockerfile`
   downloads the pinned `reranker_model`@`reranker_revision` (reads `rag_app.config.
   get_settings()` directly — no separate `ARG`, so the baked snapshot can never drift from
   what the running app asks for) into the image's Hugging Face cache at build time, then sets
   `HF_HUB_OFFLINE=1` (only after that layer, which still needs the network). Proven offline
   with a throwaway container, no access to any real data/volume: `docker run --rm --network
   none secrag-backend:ci python -c '...'` loads the baked snapshot and reranks a real
   (query, chunk) pair successfully; a model NOT baked in correctly refuses
   (`RerankerModelError: ... is not in the local cache and HF_HUB_OFFLINE is set`) — both
   checks run again in CI (`backend-image` job, ci.yml). **Image size** (`docker build`,
   same machine, back to back, before this block's Dockerfile change vs after): reported
   `docker images` SIZE 2.39 GB -> 6.05 GB; the more apples-to-apples on-disk footprint
   (`docker run --rm <image> du -sh /`, excludes `/proc`) 1.8 GB -> 3.9 GB, a **+~2.1 GB**
   delta matching the cross-encoder's single safetensors weight file (2.27 GB per `docker
   history`'s new layer) — `docker images`'/`docker save`'s own size accounting disagreed with
   each other and with `du` by a wide margin on this Docker Desktop version (containerd
   snapshotter layer-sharing display quirk, not a real discrepancy in what is actually on
   disk); `du` is the number to trust. CI's `jobs-image` build is unaffected (slim `jobs`
   image never installs torch or the reranker; confirmed `import torch` still fails there)
   and `docker compose config`/`docker compose build backend` both still succeed unchanged
   (the compose `backend` service's `build: ./backend` needed no edits). **Azure impact**
   (for the runbook, orchestrator): the first chat after any cold start (scale-to-zero wake,
   a new revision) no longer downloads the reranker from the Hub — it was already in the
   image — removing one more cold-start variable before T11.4.3's custom Ollama image and
   T11.4.4's chained cold-start measurement; no app setting needs to change (`HF_HUB_OFFLINE`
   is baked into the image itself, not read from an env var today).
   *Landed in 11.4 (block D, DA-11bC-1):* the download layer's `COPY` was narrowed to the two
   files `get_settings()` actually needs (`rag_app/__init__.py`, `rag_app/config.py` — neither
   imports any other `rag_app` module, guarded by a static test) and moved BEFORE the full
   `COPY src ./src`, so an ordinary code-only commit reuses the ~2.1–2.3 GB cached layer
   instead of re-downloading it (Docker's layer cache is sequential: the old order invalidated
   the download on every commit, locally and in CI, which had no registry/GHA cache at all).
   Confirmed live: a code-only change elsewhere under `src/` → `CACHED`; a change to either
   copied file or to `reranker_model`/`reranker_revision` → re-downloads, as intended. CI's
   `backend-image` job now builds through `docker/build-push-action` with a GHA layer cache
   (`type=gha`), same for the three other images CD builds (frontend, jobs, and the new
   `ollama` image below).
   *Landed in 11.4 (T11.4.3, block D):* **custom Ollama image** — `ollama/Dockerfile`: `FROM
   ollama/ollama@sha256:292ee7945dfc3d5840a181f3ab86fedb1e66703e02c8af98b50f4da56b7e278c`
   (version **0.35.1** — confirmed inside the pulled image with `ollama --version`; the newest
   stable release at pin time, one day newer than what Docker Hub's `:latest` tag pointed at,
   `0.35.0`/`sha256:2a6e883b…` — today's Azure `secrag-ollama` still runs `docker.io/
   ollama/ollama:latest`, which floats). `bge-m3` is pulled into the image's model store inside
   ONE `RUN` step (start the server, poll `ollama list` until it answers, `ollama pull
   bge-m3`, `pkill`, the only way to bake an Ollama model — there is no "pull without a
   server" mode); `ARG EMBED_MODEL=bge-m3` must equal `Settings.embed_model`, guarded by a
   test. **Proof** (throwaway `docker run --network none` container, own name, removed after):
   `ollama list` shows `bge-m3` already present; a raw HTTP POST to `/api/embed` (crafted with
   bash's `/dev/tcp` — no `curl`/`wget` ships in the base image) returns a real embedding
   vector, fully offline. **Recorded digests:** `bge-m3`'s own manifest digest, read from
   `/api/tags` inside the built image —
   `7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab` — is the SAME one the
   gate's seed staleness check already records for the native-Ollama `bge-m3`, confirming the
   registry serves one consistent manifest under that tag. **Image size** (same `docker
   images`-vs-`du` discrepancy as the backend image, same containerd-snapshotter display
   quirk — `du` is the number to trust): on-disk **~6.3 GB** (base ~3.7 GB content + the
   ~1.1 GB `bge-m3` model store), `docker images` SIZE ~11.6 GB. Build time ~20–40 s locally
   once the base layers are cached. CI's new `ollama-image` job (ci.yml) runs the same build +
   offline-embed proof (measured locally only so far; the real CI run's wall-clock/disk
   numbers are not yet in this ADR — add them once CD actually runs for this phase, per the
   promotion checklist). **CD order:** `secrag-ollama` is built, pushed and deployed BEFORE
   `secrag-backend`/`secrag-frontend` — the backend calls Ollama synchronously for embeddings
   on every chat/ingestion request, so Ollama must already be serving the baked-in model
   before the backend's new revision can safely take traffic (the same "dependency before
   dependent" reasoning as the migration Job running before the apps, decision 3). Always
   built and deployed on every real CD run, same as the other three images (no extra
   change-detection logic was added just for this one, for consistency). **Runbook/promotion
   items** (not applied here — orchestrator-owned, see PHASE_STATUS "Block D results" for the
   full list): the new GHCR package must be public before the first pull; the Azure
   `secrag-ollama` app's image reference switches from `docker.io/ollama/ollama:latest` to the
   GHCR digest; rollback = the previous Ollama digest, alongside the backend's (T11.6b.3).
   *Landed in 11.4 (block D, deferred from 11a):* **post-deploy health check** —
   `scripts/cd/health_check.sh` polls a URL for a bounded timeout and compares the HTTP
   status (no `-f`: a non-2xx response must still be read and compared, not swallowed; only a
   real connection failure falls back to `000`); CD's `deploy` job resolves the backend's and
   the frontend's public ingress FQDNs and polls `/health` and `/` respectively, AFTER the
   Container Apps are updated and BEFORE the purge/backup Jobs' images are updated — a broken
   revision now fails the CD run instead of going live unnoticed. No secrets: only the URL and
   the resulting HTTP status are ever printed, never a response body; every value reaches the
   script through `env:`, never pasted into a `run:` script (DA-C2-3).
   *Landed in 11.3 (DA-11bA-2, block D follow-up):* `Settings.metrics_bind_addr` (default
   `0.0.0.0`, needed by compose's separate `prometheus` container) makes the metrics server's
   bind address configurable; recommended (not applied) for Azure once a same-pod/sidecar
   scraper is the only reader — nothing reads this port on Azure today (no managed
   Prometheus, R6-1). Confirmed CD never touches a Container App's ingress at all (`az
   containerapp update` is only ever called with `--image`; ingress is configured once, by
   hand, at `az containerapp create` time per the runbook, and only ever targets port 8000),
   so this setting cannot change what is internet-reachable either way.
   *Investigated (not a code fix, block D):* the detached `secrag/gate-full` publisher's log
   occasionally shows "waiting for `<sha>`" with no later "published" line (2 of ~17 entries
   across 11a/11b so far). Ruled out: a WSL-VM idle-teardown killing the backgrounded process
   (disproven empirically — a `setsid`/`nohup` worker survived a controlled 100 s+ gap with no
   `wsl` process attached) and `gate_publish.sh`'s own `cp -f` racing a still-running
   publisher's script file (bash caches the whole script after its first read; a reproduction
   confirms a later overwrite does not affect an already-running instance). Most likely cause:
   a genuine OS/VM-level interruption (machine sleep, a Docker Desktop/WSL restart, a reboot)
   kills the detached process outright, with no chance to log anything — an environmental
   limitation, not a logic bug, and not reproducible by a test. Mitigated with a visibility
   fix: the publisher's own pid is now recorded in its log line, and a LATER
   `gate_publish.sh` run detects a dead, still-unresolved prior entry (double-checked live, in
   case it was published some other way since) and prints a clear warning in its own attended
   output instead of leaving it silently buried in a log file nobody checks automatically.
   *Landed in 11.4 (T11.4.4, block E) — measurements, methodology, floor result.*
   **Methodology (per the DA review of block C).** `eval/latency_baseline.json` ("before",
   `b77f69f`) is **untouched** — frozen exactly as committed. `rag_app.eval.latency` gained
   `reranker_mode` (`fresh`/`shared`/`warm-single`): a routine run (gate step `latency`, no
   flag) now defaults to **`shared`** — the real `reranking.get_shared_reranker()`, warmed
   once before the golden-set loop exactly like the FastAPI lifespan does — instead of the
   old fresh-per-item shape, so `rerank_inference` reflects genuine production steady-state
   cost ("after"). `warm-single` (a throwaway instance of the same class, warmed the same way
   but never the production singleton) gives an honest "before, warm" reference, isolated
   from the DI wiring. Two new one-off CLI modes: `--warm-vs-cold` (first `.rerank()` on a
   fresh instance vs later already-warm calls, same instance, same real candidates) and
   `--concurrency N` (N threads sharing the warmed instance, pooling `rerank_inference`
   latency — DA-11bC-2's contention number). `scripts/gate.sh` gained an opt-in
   `LATENCY_EXTRA_ARGS` env hook (unset by default) so these one-off runs reuse the isolated
   gate project instead of reimplementing its stack setup.

   **Concurrency correctness (DA-11bC-2), before the contention numbers below.**
   `get_shared_reranker()`'s double-checked locking only proved one-time *construction*
   (T11.4.1); `CrossEncoder.predict()` itself has no lock, and both `/chat`'s threadpool and
   `/chat/stream`'s worker thread call it on the same instance. Two new tests prove correct,
   independent results under REAL overlap (a fake model sleeps inside `predict()`, releasing
   the GIL, with an in-flight counter proving `max_active >= 2` — not an accidentally-
   serialized pair of calls): `test_reranking.py` (two threads, `.rerank()` directly, two
   different queries/candidate sets, no cross-talk) and `test_chat_stream.py` (a REAL `/chat`
   and a REAL `/chat/stream` request, through the actual API and DB harness, about two
   different documents, fired at the same time — only the external Ollama/cross-encoder
   calls mocked — each gets back its own correct answer).

   **Measurements (development machine, CPU reranker — Azure-relevant per the plan; native
   Ollama; isolated gate project, real 322-chunk corpus; `top_k`=20, `rerank_top_n`=4,
   unchanged from the baseline).** All times ms unless noted.

   | Stage | Before (fresh, frozen baseline) p50 / p95 | Before, warm (`warm-single`) p50 / p95 | After (`shared`, routine run) p50 / p95 |
   |---|---:|---:|---:|
   | `rerank_load` | 1085.9 / 1137.5 | 0 (after call 1) | 0 (never recorded — warmed before the loop) |
   | `rerank_inference` | 7902.3 / **9191.2** | 8006.5 / 9450.9 | 7881.9 / **9059.0** |
   | `generate` | 802.3 / 2759.8 | 824.6 / 2699.6 | 817.5 / 2633.8 |
   | `ttft` | 9959.9 / 11244.8 | 8772.5 / 10173.1 | 8562.0 / 9993.9 |
   | `total` | 10117.0 / 11456.3 | 8945.8 / 10188.5 | 8674.9 / 10140.5 |

   (n=42 for every stage above — 14 golden questions × 3 runs; `groundedness` n=30, unchanged
   shape, not reproduced here — see `eval/latency_results.json`/`latency_before_warm.json`.)

   **Reading the table honestly.** `rerank_load` is fully eliminated (every real request after
   the first pays zero reload cost, as T11.4.1 already proved informally in block C) — this
   alone accounts for essentially all of the ~1.1–1.4 s drop in `total`/`ttft` p50 between
   "before" and "after"/"before, warm". **`rerank_inference` itself barely moves**
   (9191.2 → 9059.0 ms p95, a 1.4 % drop) **and "before, warm" is statistically
   indistinguishable from "after"** (9450.9 vs 9059.0 ms p95, within run-to-run noise) — i.e.
   sharing/warming the reranker removes the *reload* tax but does essentially nothing for
   `rerank_inference`'s own cost, because that cost was never a warm-up artifact to begin
   with. Confirmed directly by the **warm-vs-cold sanity check** (one real golden item, 20
   real candidates, a fresh un-warmed instance): call 1 (cold) — `rerank_load` 3362.8 ms +
   `rerank_inference` **5903.4 ms**; calls 2–5 (same instance, now warm) — `rerank_load` 0,
   `rerank_inference` mean **5824.1 ms** (range 5732.5–5926.2) — essentially the SAME number,
   confirming `rerank_inference` is genuine CPU compute over `top_k`=20 candidates near the
   512-token cap (bge-reranker-v2-m3, ~568 M params, fp32, no GPU), not a repeated warm-up
   artifact. (Block C's own informal "485–505 ms steady-state" number used short placeholder
   pairs, not real candidate-length text at this `top_k`/`max_length` — not comparable to the
   figures here; this block's number is the one the floor below is checked against.)

   **Floor (T11.4.4 Done-when): rerank p95 ≤ 50 % of baseline AND ≤ 1.5 s, on CPU.**
   `rerank_inference` p95 "after" = **9059.0 ms**: 98.6 % of baseline (9191.2 ms, needs
   ≤ 4595.6 ms) and 6× the 1.5 s absolute ceiling. **FLOOR: FAIL, on both the relative and the
   absolute test.** Per the task: not tuned here (T11.5/block F's job — the reranker/`top_k`
   experiment matrix is exactly the lever this floor needs); recorded honestly as a hard
   finding for block F, not swept into "T11.4.1 already fixed it" — T11.4.1 fixed the reload,
   it did not and could not fix the CPU inference cost itself.

   **Concurrency contention (DA-11bC-2's number; `--concurrency N`, N threads sharing the one
   warmed instance, pooling `rerank_inference` across the full golden set once per thread):**

   | Concurrency | n | p50 | p95 | wall time |
   |---|---:|---:|---:|---:|
   | 1 (the "after" row above) | 42 | 7881.9 | 9059.0 | 362.2 s (×3 runs) |
   | 2 | 28 | 10609.3 | 12209.6 | 138.5 s |
   | 4 | 56 | 25196.1 | 28829.5 | 333.4 s |

   CPU cross-encoder inference contends hard under concurrent load: p95 is +35 % at 2
   concurrent requests and +218 % at 4 (≈29 s per answer's rerank stage) — no effective
   parallel speed-up on this 28-thread CPU once PyTorch's own intra-op threads are already
   saturated by one call, consistent with the single-threaded cost already being CPU-bound.
   Relevant for Azure concurrency sizing (not tuned here — T11.5 chooses the reranker/`top_k`,
   16.3 sizes the deployment against the result).

   **Backend RSS** (development machine, `secrag-backend:e5` built from this block's code,
   throwaway gate-project DB with the real corpus, native Ollama; `/proc/1/status` VmRSS of
   the single uvicorn process — `docker stats`' cgroup-based number reads far lower, 756 MiB,
   the same containerd/cgroup accounting gap already flagged for image sizes in blocks C/D,
   most likely mmap'd model-weight pages counted as reclaimable file cache rather than
   resident anonymous memory; VmRSS is the number to trust):
   idle after warm-up **2.02 GiB** (2,064,448 KB); after a 4-concurrent-request `/chat` burst
   **2.32 GiB** resident (2,432,160 KB), peak (`VmHWM`) **3.72 GiB** (3,903,408 KB) during the
   burst. The ~2 GiB floor is essentially the reranker's own fp32 weights (~2.2 GB on disk,
   T11.4.2); concurrent requests add real activation memory on top (confirmed by the VmHWM
   jump), consistent with the contention numbers above.

   **Local cold start** (container start → first successful readiness response; same
   throwaway gate-project DB + native Ollama for the backend; `secrag-ollama:e5`/
   `secrag-frontend:e5` built from this block's `ollama/Dockerfile`/`frontend/Dockerfile`):
   **backend** (`/health` 200, includes DB connectivity checks + the reranker's full
   load-and-warm-up) 7.36 s on a cold OS page/disk cache (first run), 4.70–4.82 s once the
   image's layers are already cached in RAM (two subsequent runs) — both numbers are "local
   cold start", Azure's own node-level disk-cache state is unknown and not assumed either
   way; **Ollama** (`/api/embed` succeeds for `bge-m3`, already baked in, T11.4.3) 1.61 s end
   to end (server ready in 0.44 s + first real embed ~1.2 s; a later, warm embed call: 25 ms);
   **frontend** (first successful `GET /`) 0.39–0.44 s (Next.js standalone server start).

   **Chained cold-start estimate vs the Azure ingress timeout.** The real dependency chain on
   a cold wake is sequential, not parallel: the backend only calls Ollama once it is already
   serving and reaches the embed step of its first real request (not during its own startup),
   so worst case ≈ frontend + backend + Ollama summed: **cold-disk-cache worst case
   ≈ 0.44 + 7.36 + 1.61 ≈ 9.4 s**; **warm-disk-cache ≈ 0.4 + 4.8 + 1.6 ≈ 6.8 s**. Both are
   roughly **25–35× under** the Azure Container Apps ingress timeout (**~240 s**, PHASE_TASKS
   row 42 / DA-G3-4) — the baked images (T11.4.2/T11.4.3) remove essentially all cold-start
   risk of hitting that cut; the real risk this phase found is `rerank_inference`'s own
   per-request CPU cost under concurrent load (above), not cold start.

   **Data safety:** every measurement ran against the isolated `secrag-gate` project (its own
   `secrag_gate_pgdata` volume, torn down with `down -v` afterwards) or a throwaway,
   unpublished image tag (`secrag-backend:e5`/`secrag-ollama:e5`/`secrag-frontend:e5`, removed
   at the end); `rag_ia_pgdata`'s `CreatedAt` was unchanged throughout (confirmed before and
   after); no Azure command.
9. Promotion hardening found while building 11a (R6-5 and the stream findings).
   *Landed in 11.2 (T11.2.15–17):* **log hygiene** — the emailer logs neither the address
   nor the link; the uvicorn access log redacts every query value (`/auth/verify?token=
   [redacted]`); every app engine hides bound SQL parameters; a racing duplicate sign-up is
   a 409 with nothing logged; Postgres runs with `log_error_verbosity=terse` (compose, gate;
   Azure at the promotion); the verification link is returned by the API only with
   `ENV=dev` (Azure has no SMTP until 12b, so returning it would let anyone verify an
   address they do not own). **Stream errors** — the first SSE event carries the
   `conversation_id`; every failure ends the stream with one `error` event, the failed turn
   is stored as an assistant error marker, a client that goes away gets an `interrupted`
   marker, `: keep-alive` comments every 15 s keep proxies from cutting a long stage, and no
   database connection stays idle in a transaction while the model generates; one
   undecryptable row never fails the conversation list. **Dependencies** — FastAPI/starlette
   and sentence-transformers/transformers on current releases, torch pinned, the reranker
   pinned to a full Hub commit and loaded offline when cached; `dependency-audit` has no
   interim Python exception left (postcss in Next only). The evaluation did not move:
   recall 1.0 · faithfulness 1.0 · correctness 0.9 · abstention 1.0 before and after.
10. Container Apps environment: Express → standard (workload profiles). *Done on Azure on
    2026-10-01, before the promotion.* **Why:** the Phase 10 environment `secrag-env` turned
    out to be an **Express** environment — it rejects Container Apps Jobs (decisions 3, 5 and
    6 need three of them) and revision suffixes, and it has no workload profiles. Express
    quirks met while preparing the promotion: `az containerapp revision restart` failed with
    `InternalServerError`; changing an app's environment variable did not restart the running
    replica; `printenv` through `az containerapp exec` printed nothing, so a variable is read
    with `python -c "import os; print(os.environ.get(…))"` instead. **How:** a new standard
    environment **`secrag-cae`** in the same resource group and region (Spain Central), on the
    same Log Analytics workspace, with the **Consumption** workload profile. az 2.90's
    `containerapp env create` cannot ask for an environment mode (the resource provider then
    defaults to Express), so it was created with `az rest --method put` on
    `…/managedEnvironments/secrag-cae?api-version=2026-07-01` with
    `properties.environmentMode = WorkloadProfiles`, and the mode was read back. **Gate G**
    before any app moved: a throwaway Schedule Job (`0 * * * *`, Consumption profile) was
    created and started in the new environment, and reachability probes passed; on failure
    only the new, empty environment would have been deleted. Then the three apps were
    recreated there with the **same names** (min 0 / max 1): the backend on the pre-11a digest
    with the uvicorn-only override of the [Rollback](#rollback) pre-step and
    `DAILY_ANSWER_CAP=150`; the frontend rebuilt from the deployed commit (`dee9cbc-cae`) with
    the new backend URL baked in (`NEXT_PUBLIC_API_URL` is build-time, ADR phase 10; the
    repository variable was updated for CD); Ollama pinned by digest (0.35.0). The old
    environment was deleted after the smoke. **Consequences:** the public URLs changed (new
    default domain; any old link to the Express domain is dead, and so is every frontend image
    that baked it); **one** environment hosts the apps and the Jobs; the
    subscription's quota allows one standard environment in the region, so a second one (for
    example, a blue/green move) means deleting this one first; the Consumption profile keeps
    scale-to-zero billing, so the cost model under [Costs](#costs) is unchanged. Every
    `az containerapp update` in this ADR is written without a revision suffix — the form that
    was tested.

## Key recovery (D-2026-09-29-2)

Class (b) accounts (1 of 5 locally, 1 of 2 on Azure) get a time-boxed recovery before R5-5
erases them: the user supplies candidate old master keys, a local tool tests them, a match is
re-wrapped under the **current** key (the master key is never switched back — newer accounts
depend on it), and whatever still fails is purged with a tombstone before the first
fingerprint write.

**Tool.** `scripts/azure/key_recovery.sh` (backend venv, `python -I -B`, core dumps off) →
`key_recovery.py`. Before it reads any key, the process sets `RLIMIT_CORE` to 0 **and**
`prctl(PR_SET_DUMPABLE, 0)` (no core dump even with a pipe `core_pattern` such as
systemd-coredump or a WSL crash collector; no same-user ptrace or `/proc/<pid>/mem`); it
refuses to run if that fails.

- `init` creates `~/.secrag-recovery/candidates` (directory 0700, empty file 0600) and prints
  how to fill it **without the shell history** (`nano …`, or `cat > …`, paste, Ctrl-D). One
  key per line; `#` comments and blank lines are ignored; `DATA_MASTER_KEY=<key>` lines are
  accepted. Each line is decoded on its own as UTF-8 with a leading BOM and CR removed (a file
  saved by a Windows editor works); a line that is not UTF-8 prints `candidate #j: unreadable`
  and the others still run. `--candidates PATH` (before the subcommand) uses another file.
- **Labels.** `account #i` = position by `users.created_at, users.id` among the accounts with
  a key; every command first prints `accounts: N in total`. A new account is appended at the
  end, but an erased one shifts the labels after it, so `--expect-total N` (optional for
  `check`/`rewrap`, mandatory for `erase`) refuses when the total is no longer what `check`
  printed.
- Every run refuses a file or directory with wider permissions, not owned by the user, a
  symlink anywhere on the path, a path inside any git work tree, or one on a Windows drive
  (`/mnt`, 9p/drvfs). `.gitignore` also ignores `**/.secrag-recovery/` and `**/candidates`
  (defensive; the gitleaks allowlist is unchanged).
- `check --accounts all|1,2 [--current-key-from-app APP | --current-key-from-env-file PATH]
  [--no-candidates]` reads the wrapped keys through libpq in a READ ONLY transaction (only the requested
  accounts) and prints only `account #i: current key OK|KO` and
  `account #i: candidate #j OK|KO`, plus a summary. Accounts are numbered by
  `users.created_at, users.id`, never identified.
- `rewrap --account N --candidate J --current-key-from-… [--apply --i-have-a-pg-dump]`: one
  transaction; refuses unless the current key cannot unwrap the account and candidate J can;
  unwraps, wraps the same data key under the current master key, verifies; without `--apply`
  it prints `dry run: would update 1 row` and rolls back (works in a read-only session); with
  `--apply` (only together with `--i-have-a-pg-dump`) it locks and updates exactly that row.
- `erase --account I --expect-total N --current-key-from-env-file PATH [--apply
  --i-have-a-snapshot]` (**local development DB only**, D-2026-09-30-2 / DA-E-9): for an
  account that **no** key can unwrap. One transaction: checks the total, locks the account,
  **proves the current key is the app's key** (DA-E2-1: it must match the stored master-key
  fingerprint when one exists **and** unwrap at least one *other* account — a wrong or stale
  env file makes every account "KO", so it is refused; DA-F-1: while **no** fingerprint is
  stored, an OLD key still unwraps the accounts wrapped under it, so the current key must
  unwrap **every** other account, or `--expect-readable R` must equal the readable count that
  `check` printed with the same key **and** R must be a majority of the other accounts),
  refuses unless the current key **and
  every candidate** fail on that very blob (and while any candidate line is unreadable or
  malformed; `--no-candidates` when no candidate file exists because recovery was skipped —
  `check` accepts it too), then erases it through the app's own
  erasure path (`rag_app.erasure.erase_user`: the user row is deleted with its key,
  conversations and messages by cascade, and a `done` tombstone is written, one commit).
  Without `--apply` it is a read-only dry run. It needs the 0005 schema (the app's tombstone
  columns) and refuses a non-local `PGHOST`/`PGHOSTADDR`, `PGSERVICE`, or a run inside
  `db-tunnel.sh`: on Azure the owner deletes that account in the running app instead
  (`DELETE /account`).
- **Unknown outcome.** With `--apply`, an error after COMMIT was sent prints `… state unknown
  — run check again` instead of `nothing changed` (DA-E-4); re-running `check` shows the
  real state (`rewrap` then says "already readable").
- `shred` overwrites every file in `~/.secrag-recovery` with random bytes, then zeros
  (fsync each), deletes them and the directory.

**What never leaks.** Keys (candidates, current, wrapped, unwrapped) are never on argv or in
the environment, never printed or logged, never written to any file: errors print an
exception **class** only (no message, no traceback); a malformed candidate prints
`candidate #j: not a valid Fernet key`. On Azure the tool runs **inside**
`scripts/azure/db-tunnel.sh` (which allows exactly this script besides `psql`/`pg_dump`), so
the wrapped keys go from the server into the process' memory only, and candidates never go
to Azure. The current Azure key is read by the tool itself from the Container App secret into
memory. Tests (`backend/tests/test_key_recovery.py`, fake keys) scan stdout, stderr and every
file written under the test's `$HOME`, `/tmp`, `/var/tmp`, `/dev/shm` and the repository
during a run — also with a malformed or non-UTF-8 candidate and with a failing database or
tunnel — for any key as text or fragment, as raw bytes, and base64 / url-safe base64 / hex
encoded (a self-test proves the scan can fail); they check the refusals (modes, symlinks, git
work tree, Windows drive, `erase` targets), `shred`, the non-dumpable process
(`PR_GET_DUMPABLE`, and a root-owned `/proc/<pid>` while the tool runs), `erase` end to end,
and "state unknown" with a fault injected after COMMIT. Limits: Python cannot
wipe immutable strings from memory, and an overwrite on a journaling/SSD disk is best effort;
the WSL disk image is not encrypted, so the lasting copy of old keys belongs in the password
manager only.

**Local evidence (2026-09-30, throwaway restore of the `rag_ia` snapshot, FAKE candidates):**
`check --accounts all --current-key-from-env-file backend/.env` → accounts #2–#5 current key
OK, **account #1 current key KO**, fake candidates KO; `rewrap` dry run in a read-only session
→ `candidate #1 KO — nothing changed`. `erase` (same snapshot, second throwaway restore, FAKE
candidates): on 0004 → refused ("migration 0005"); after `roles.sql` + `alembic upgrade head`
on the throwaway: wrong total and readable account #2 refused, dry run changed nothing,
`--apply` → users/keys 5 → 4, tombstones 4 → 5 (`done`), `check --expect-total 4` → 4 of 4
readable. The throwaway containers and volumes were removed; the development volumes were
not touched.

**Decision update (D-2026-09-29-2 CHANGED, 2026-09-30):** the owner does not need the old
conversations, so **no recovery is attempted** and no candidate file is created; the path is
**erase**. On Azure the owner deletes the unreadable account in the running app and
re-registers before row 40. With no candidate file there is nothing to `shred`. The recovery
procedure further below stays documented for a future key incident.

**Local erase — the command list** (WSL login shell, repository root; run on 2026-09-30 with
N = 5, I = 1; stop at the first unexpected line):

```bash
docker ps -a --filter volume=rag_ia_pgdata --format '{{.Names}} {{.Status}}'
docker rm <stale container>   # any pre-T11.2.1 container on the volume (it was rag_ia-db-1,
                              # bound to 0.0.0.0:5432); the volume is kept, never `down -v`
# compose needs JWT_SECRET/DATA_MASTER_KEY only for interpolation: inline dummies, NEVER export
JWT_SECRET=unused DATA_MASTER_KEY=unused docker compose up -d --no-deps db
umask 077; ts=$(date -u +%Y%m%dT%H%M%SZ)
PGPASSWORD=rag pg_dump -h 127.0.0.1 -p 5432 -U rag -d rag -Fc \
  -f ~/secrag-backups/rag_ia-pre-erase-$ts.dump
pg_restore -l ~/secrag-backups/rag_ia-pre-erase-$ts.dump | grep -c 'TABLE DATA'   # > 0
JWT_SECRET=unused DATA_MASTER_KEY=unused docker compose up -d db-roles migrate
JWT_SECRET=unused DATA_MASTER_KEY=unused docker compose wait migrate    # exit 0; 0004 -> 0005
KR() { PGHOST=127.0.0.1 PGPORT=5432 PGUSER=rag PGPASSWORD=rag PGDATABASE=rag \
  scripts/azure/key_recovery.sh "$@"; }
KR check --accounts all --no-candidates --current-key-from-env-file backend/.env
#   → "accounts: N in total"; only account #I KO
KR erase --account I --expect-total N --no-candidates --current-key-from-env-file backend/.env
#   → "current key verified: unwraps N-1 of N-1 other account(s); fingerprint not stored yet"
KR erase --account I --expect-total N --no-candidates --current-key-from-env-file backend/.env \
  --apply --i-have-a-snapshot
KR check --accounts all --expect-total N-1 --no-candidates \
  --current-key-from-env-file backend/.env                              # N-1 of N-1 readable
env | grep -c '^DATA_MASTER_KEY='   # must be 0: an env var would override backend/.env
(cd backend && PYTHONPATH=src .venv/bin/uvicorn rag_app.api.app:app --host 127.0.0.1 --port 8000)
#   first start → /health ok, fingerprint stored; Ctrl-C
PGPASSWORD=rag psql -h 127.0.0.1 -U rag -d rag -tAc 'SELECT count(*) FROM master_key_fingerprint'  # 1
```

- **Pre-erase dump**: plaintext (`pg_dump -Fc`, not age), mode 0600 in `~/secrag-backups`
  (next to the read-only volume snapshots) — **not** `~/secrag-db-backups`, whose pruner only
  knows `secrag-<ts>.dump.age` names and would never delete it. It expires with the
  snapshots (X9, 14 days; deleted by hand). Rollback = restore it into a NEW empty database:
  it is schema 0004, and the 0005 downgrade is not a rollback path.
- `migrate` and `erase` both run as `rag`, the compose owner; `erase` connects through libpq
  on 127.0.0.1:5432 (`--no-deps db` binds it there only).
- **First API start**: native (it reads `backend/.env`, the very key `erase` verified), with
  no `DATA_MASTER_KEY` in the shell; compose would need the real key exported. A start with
  any other key fails closed (R5-5: stored keys do not unwrap, or the fingerprint differs).

**Procedure** (the user's real candidates; nothing is run on the developer's database before
it; note the total `N` that `check` prints):

- Local — restore the snapshot into a throwaway container and `check --accounts all
  --current-key-from-env-file backend/.env`. On a match: `pg_dump` of the developer's
  database, then `rewrap --account I --candidate J --expect-total N … --apply
  --i-have-a-pg-dump` on it, then `diag_keys.py` → KO 0. No match: `pg_dump`, `roles.sql` +
  migration 0005 (compose `db-roles`, `migrate`; they read no user key), `erase --account I
  --expect-total N --current-key-from-env-file backend/.env` (dry run), then the same with
  `--apply --i-have-a-snapshot`, then `check --accounts all --expect-total N-1` → all OK —
  before the first API start (which writes the fingerprint only when every key unwraps); the
  exact commands are the local erase list above.
- Azure (row 40, orchestrator) — `db-tunnel.sh --password-from-app --
  scripts/azure/key_recovery.sh check --accounts all --current-key-from-app secrag-backend`
  (read-only, any time); on a match, after the row-40 `pg_dump`, the same `rewrap` with
  `--read-write` on the tunnel; re-run the row-15 snippet → KO 0. No match: the owner deletes
  the account in the running pre-11a app before the promotion (`erase` refuses the tunnel).
- `shred` only **after both outcomes** — the local one (rewrap or erase) **and** the Azure one
  (row-40 rewrap or the owner's deletion) — because the Azure `rewrap` still needs the
  candidate (DA-E2-2); shredding earlier means refilling the file from the password manager.

## Rollback

*Written before each promotion.* **11a — written 2026-10-01, revised the same day after the DA
pre-promotion review (DA-P-1, 3, 5, 9, 10); reviewed by the user before the Azure pre-merge.**

**Principles.**

- **Images roll back, the schema does not.** 0005 is expand-only (DoD, expand/contract), so
  the pre-11a image runs on it. The 0005 downgrade is **not** a rollback path: it refuses as
  soon as one account has been erased, and it would drop the tombstone bookkeeping.
- **CD never rolls back.** It skips a SHA that is already deployed, older, or not the tip of
  `main`. A rollback is a **manual `az containerapp update` to the previous digests** (below);
  the lasting fix is a **forward-fix commit** through the normal gate. A plain `git revert`
  of the 11a merge is not a rollback either: the reverted tree has no `0005` file, so the
  migration Job fails ("Can't locate revision") and CD stops before the apps — safe, but it
  deploys nothing — and the reverted backend `CMD` would migrate at start again.
- **Additive changes stay:** the Azure data fix (the owner's unreadable account deleted),
  PITR, the database roles and grants, the Storage account and its blobs, the Jobs' identity,
  the `master_key_fingerprint` row (the old image ignores it).

**Previous images** (recorded 2026-10-01 with read-only `az containerapp show` and
`docker buildx imagetools inspect` of the deployed tags; updated the same day for the move to
`secrag-cae`, decision 10): the backend (single revision mode, min 0 / max 1) runs
`rag_app-backend:dee9cbc…`, whose `CMD` is `sh -c "alembic upgrade head && uvicorn …"`. The
previous frontend is the `dee9cbc-cae` rebuild, which bakes the `secrag-cae` backend URL; the
original `dee9cbc` frontend digest (`6706e998…`) bakes the deleted Express domain and must
never be rolled back to.

```bash
RG=rg-secrag
OLD_BACKEND=ghcr.io/davidmorgadocarames/rag_app-backend@sha256:dfbb568bfcd6be33a396a36ceb9702db3d55ce30d1872101ea01bbdc68394b1e
OLD_FRONTEND=ghcr.io/davidmorgadocarames/rag_app-frontend@sha256:e924953b88a845c7b9bfb336784ed14fcbaa8fce995919031648281d946f2181
FQDN=$(az containerapp show -g $RG -n secrag-backend --query properties.configuration.ingress.fqdn -o tsv)
```

**The uvicorn-only command override.** `--command "env" --args "UVICORN_HOST=0.0.0.0"
"UVICORN_PORT=8000" "uvicorn" "rag_app.api.app:app"`: `env` sets uvicorn's own host/port
variables and `exec`s uvicorn (no shell; uvicorn is PID 1 and gets SIGTERM directly), which
is the 11a image's `CMD`. It contains no argument that starts with `-` on purpose: az 2.90
reads a leading `-` inside `--args` as one of its own options (`--command "/bin/sh" --args
"-c" …` fails with "unrecognized arguments", DA-P-1). Checked 2026-10-01: it parses in az
2.90; in the **pre-11a digest above** over a 0005 database it starts (`Uvicorn running on
http://0.0.0.0:8000`, `/health` 200), while that image's own `CMD` exits 255 there ("Can't
locate revision 0005"); in the **11a image** it starts with the fingerprint written. Both
images have `WORKDIR /app`, `PYTHONPATH=/app/src` and no `ENTRYPOINT`, so no `--app-dir` is
needed, and the override adds no container environment variable (gotcha 7 does not apply).
No revision suffix is passed (decision 10): Azure names each new revision itself, so a
repeated command never collides with an existing revision name.

**Pre-step, before the first real CD run (D-2026-09-30-4).** Once the migration Job has moved
the database to 0005, the pre-11a image's own `alembic upgrade head` fails on a revision it
does not know, so **every cold start** (min replicas 0 → every wake-up) would crash-loop
between CD's `migrate` and `apps` steps, and for as long as a failed `apps` step leaves the
old revision in place. So, in the pre-merge, the running backend gets the **same image with
its command overridden to uvicorn only** (done 2026-10-01: the backend recreated in
`secrag-cae`, decision 10, was created with this override; the check below still applies):

```bash
az containerapp update -g $RG -n secrag-backend --image "$OLD_BACKEND" \
  --command "env" --args "UVICORN_HOST=0.0.0.0" "UVICORN_PORT=8000" "uvicorn" "rag_app.api.app:app"
az containerapp show -g $RG -n secrag-backend \
  --query "properties.template.containers[0].{image:image,command:command,args:args}" -o json
# expect image = $OLD_BACKEND, command = ["env"],
#        args = ["UVICORN_HOST=0.0.0.0","UVICORN_PORT=8000","uvicorn","rag_app.api.app:app"]
curl -fsS "https://$FQDN/health"   # wake-up; then log in and open a conversation
```

This also rehearses the rollback command below on the live app (nothing has changed in the
database yet, so a failure here simply stops the promotion). The override stays in the
template when CD later updates the image; it is equivalent to the 11a image's own `CMD`, so it
is kept.

**Health gate after CD = the rollback trigger (DA-P-5).** CD's `update` returns when the
revision is provisioned, not when the app is healthy, so a backend that refuses to start
(invalid settings, master-key fingerprint, R5-5) still leaves a green run. Right after CD, and
**before** the purge/backup crons are switched on:

```bash
curl -fsS "https://$FQDN/health"   # wakes the new revision; must answer 200
az containerapp revision list -g $RG -n secrag-backend \
  --query "[?properties.active].{name:name,health:properties.healthState,running:properties.runningState,image:properties.template.containers[0].image}" -o table
# expect one active revision on the NEW backend digest, Healthy, Running
# master_key_fingerprint has exactly 1 row (db-tunnel.sh … psql -c "SELECT count(*) FROM master_key_fingerprint")
```

Any of the three failing is the **main trigger**.

**Main trigger — "migrate OK, apps failed or unhealthy".** CD's migration step is green
(database at 0005), then the apps step fails, or the health gate above fails, or the Azure
smoke finds a fault that blocks users. **Not a trigger (DA-P-10):** the apps updated and pass
the health gate and only CD's last step ("Purge and backup Job images") failed — the
deployment shows `failure`, but users are fine: **roll forward** — set the two Job images by
hand from the migration Job's digest (step 3 below, without the delete) and continue.

1. **See what is running:** `az containerapp revision list -g $RG -n secrag-backend -o table`
   and the `show` query above. If the backend still runs `$OLD_BACKEND` with the override
   (the update never applied), there is nothing to roll back for the apps: users are on the
   old app over 0005 (effects below) — do step 3 and fix forward.
2. **Roll both apps back together** (the new frontend expects the 11a API, the old one the
   old API):

   ```bash
   az containerapp update -g $RG -n secrag-backend --image "$OLD_BACKEND" \
     --command "env" --args "UVICORN_HOST=0.0.0.0" "UVICORN_PORT=8000" "uvicorn" "rag_app.api.app:app"
   az containerapp update -g $RG -n secrag-frontend --image "$OLD_FRONTEND"
   ```

   Then the scale check (min 0 / max 1 on every app), `/health`, a login and a conversation
   read, and a note of the time (the window of the effects below).
3. **Jobs (DA-P-3, D-2026-10-01-4).** The main trigger fails CD **before** its last step, so
   the purge and backup Jobs still run the placeholder image on the dormant cron. The
   migration Job is the only one that carries the deployed jobs digest — read it **before**
   deleting that Job:

   ```bash
   JOBS_IMAGE=$(az containerapp job show -g $RG -n "$AZURE_MIGRATE_JOB" \
     --query "properties.template.containers[0].image" -o tsv)
   echo "$JOBS_IMAGE"   # must be ghcr.io/davidmorgadocarames/rag_app-jobs@sha256:…; else take it
                        # from the CD run's image digests and stop until it is known
   for job in "$AZURE_PURGE_JOB" "$AZURE_BACKUP_JOB"; do
     az containerapp job update -g $RG -n "$job" --image "$JOBS_IMAGE"
     az containerapp job show -g $RG -n "$job" \
       --query "{image:properties.template.containers[0].image,cron:properties.configuration.scheduleTriggerConfig.cronExpression,timeout:properties.configuration.replicaTimeout,retry:properties.configuration.replicaRetryLimit}" -o json
   done
   az containerapp job update -g $RG -n "$AZURE_PURGE_JOB" --cron-expression "0 * * * *"
   az containerapp job update -g $RG -n "$AZURE_BACKUP_JOB" --cron-expression "0 3 * * *"
   az containerapp job delete -g $RG -n "$AZURE_MIGRATE_JOB" --yes
   ```

   The **purge Job is kept**: it runs the new jobs image against 0005 independently of the
   app image and keeps the 24 h promise of every 202 already sent; the tombstones that the old
   image's synchronous `DELETE /account` writes get the 0005 default `pending`, and since that
   user row is already gone the purger closes them as `done` with 0 rows. The **backup Job is
   kept** too (D-2026-10-01-4: it only reads as `secrag_backup`, does not depend on the app
   image, and keeps the 14-day copies flowing). The **migration Job is deleted**: no one can
   start a migration while the apps are rolled back; the next attempt re-creates it from YAML
   (placeholder image, then `job update --image` to the deployed digest, X1, and the owner's
   secret). The **Storage account and its blobs are kept** (encrypted; the 12-day lifecycle
   rule keeps running).
4. **Forward fix:** a new commit on `main` through the full gate; CD compares it with the last
   *successful* deployment and redeploys. Re-create the migration Job first (the migration
   step needs it).

**Accepted short-window effects of the old image on 0005** (keep the rollback short):

- It does not filter `deleted_at`: an account erased by the 11a app whose JWT is still valid
  (≤ 30 min) can still authenticate; its data stays unreadable (the key is gone) and it can
  never log in again (email and password are NULL).
- `/auth/me` for such an account → 500 (the old response model requires an email).
- No daily answer cap (the old code ignores `usage_daily`): only the 10K TPM deployment cap
  and the budget alert bound the spend.
- The old emailer logs the address and the link, and the old resend endpoint returns the
  verification link with no SMTP: the R6-5 log promise and email verification do not hold
  while rolled back.
- The old stream has no `conversation`/`error`/keep-alive events; `DELETE /account` is the
  old synchronous delete (one long transaction; fine at today's data size).
- The old image ignores the master-key fingerprint: no secret is changed while rolled back.

**Data problems are not image rollbacks:** a wrong data fix is repaired from the row-40
`pg_dump` or PITR (≤ 14 days) into a **new** server/database, never over the live one;
roles, grants and Storage are additive and need no rollback.

## Costs

*Measured the day after each promotion* (Cost Management query) and compared with the caps:
11a ≈ €0.20 (cap €1), 11b ≈ €0.15 (cap €1). 11a adds recurring Jobs (hourly purge ≈
€0.13/month, daily backup ≈ €0.03/month, slim image, scale to zero between runs) and one
Storage account (a few MB of encrypted dumps, 12-day lifecycle); the migration Job runs only
during a deploy. The measured value is added after the promotion.

### Worst-case LLM cost per answer and the daily answer cap (R6-1, DA-31b-3)

`DAILY_ANSWER_CAP` counts **answers**, not tokens, so the cost of one answer must be bounded.
Every LLM call passes `max_tokens` (a unit test scans the code for calls without it):

| Call (per answer) | Input bound | Output bound |
|---|---|---|
| Generation | system prompt + 4 reranked chunks + the question | `max_tokens` = **1024** (`Settings.max_tokens`, unchanged) |
| Groundedness check | system prompt + the same 4 chunks + the answer | **16** (`GROUNDEDNESS_MAX_TOKENS`; the verdict is one word) |

Before this fix the groundedness call had no `max_tokens`, so its output was bounded only by
the model's own limit (32K tokens for gpt-4.1-mini). Embeddings run on Ollama (no Azure cost);
the chit-chat reply makes no LLM call; the query rewrite and the eval judge are CLI/eval only.

**Token bound.** The corpus chunks are ≤ 1,352 characters (322 chunks, mean 651;
`chunk_size` 1200 + overlap), so a chunk with its `source | version` header is ≤ ~1,450
characters. The question is ≤ 2,000 characters (API validation). The worst case counts **one
token per character** (a strict upper bound for this English corpus; ~4 characters per token
is typical):

- generation in ≈ 100 (system) + 4 × 1,450 + 2,000 + 80 ≈ **8,000**, out ≤ **1,024**;
- groundedness in ≈ 40 + 4 × 1,450 + 1,024 (the answer) + 40 ≈ **6,900**, out ≤ **16**;
- **per answer ≤ ~14,900 in + 1,040 out.**

**Price** (Azure OpenAI gpt-4.1-mini, Global Standard list price at the time of writing:
$0.40 per 1M input tokens, $1.60 per 1M output tokens; re-check the Azure pricing page at
row 40, a regional/Data Zone deployment costs slightly more):

| Scenario | $ / answer | 300/day: $/day | 300/day: 30 days | 150/day: $/day | 150/day: 30 days |
|---|---|---|---|---|---|
| **Worst case** (1 token/char, max question, 1024-token answer) | 0.0076 | 2.29 | **68.6** | 1.14 | **34.3** |
| Heavy (~4 chars/token, max question, 1024-token answer) | 0.0035 | 1.04 | 31.1 | 0.52 | 15.6 |
| Typical (mean chunk, short question, ~300-token answer) | 0.0013 | 0.39 | 11.7 | 0.20 | 5.9 |

Worst case: 14,900 × $0.40/1M + 1,040 × $1.60/1M = $0.00596 + $0.00166 = $0.0076. The
30-day columns assume the cap is **exhausted every day** (sustained abuse); the per-IP rate
limiter and the deployment's 10K TPM quota do not bind first (300 × 15K ≈ 4.5M tokens/day is
below 10K TPM × 1,440 min ≈ 14.4M). Against the $86 student credit, 300/day at the worst case
is ~80 % of it in 30 days; 150/day is ~40 %. **Decision (D-2026-10-01-1): 150/day** on
Azure, which is also the code default (a lost env var cannot raise it); demo traffic is far
below it, and raising it later is one change of the existing variable plus a check in the
running revision (`az containerapp exec` with `python -c`, since `printenv` printed nothing on
the Express environment, decision 10). The real average per answer is `usage_daily.tokens / answers` (a
lower bound, DA-31b-4) after the first Azure week.
