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
   *Landed in 11.2 (T11.2.1):* `docker-compose.yml` declares `pgdata` as the **external**
   volume `rag_ia_pgdata` — the one that held the data (the 11.1 diagnosis) — so its name no
   longer depends on the compose project, and compose never creates, recreates or removes it
   (`down -v` included). `scripts/dev/create_dev_volume.sh` creates it once on a new machine
   and never touches an existing one. The gate never references it: `compose.gate.yml` has
   its own volume, and `restart_check.sh` swaps it for a throwaway volume and refuses any
   development volume. The stray `rag_app_pgdata` (schema + corpus only, no user rows) is no
   longer used; removing it is a manual, verified step.
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
   versioned YAML (`deploy/azure/jobs/*.yaml`: Manual trigger, placeholder image and
   secrets, parallelism 1, timeout, retry limit) applied by hand — CD never applies YAML or
   touches schedules. A dry run prints the same order step by step, and the dispatch input
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
6. Asynchronous erasure: short request transaction (crypto-shred + PII scrub) → 202, then a
   batched, resumable purger.
7. Global daily answer cap.
8. (11b) One shared reranker, baked model images, latency gate.

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
- `check --accounts all|1,2 [--current-key-from-app APP | --current-key-from-env-file PATH]`
  reads the wrapped keys through libpq in a READ ONLY transaction (only the requested
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
  refuses unless the current key **and every candidate** fail on that very blob (and while
  any candidate line is unreadable or malformed), then erases it through the app's own
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

**Procedure** (the user's real candidates; nothing is run on the developer's database before
it; note the total `N` that `check` prints):

- Local — restore the snapshot into a throwaway container and `check --accounts all
  --current-key-from-env-file backend/.env`. On a match: `pg_dump` of the developer's
  database, then `rewrap --account I --candidate J --expect-total N … --apply
  --i-have-a-pg-dump` on it, then `diag_keys.py` → KO 0. No match: `pg_dump`, `roles.sql` +
  migration 0005 (compose `db-roles`, `migrate`; they read no user key), `erase --account I
  --expect-total N --current-key-from-env-file backend/.env` (dry run), then the same with
  `--apply --i-have-a-snapshot`, then `check --accounts all --expect-total N-1` → all OK —
  before the first API start (which writes the fingerprint only when every key unwraps).
- Azure (row 40, orchestrator) — `db-tunnel.sh --password-from-app --
  scripts/azure/key_recovery.sh check --accounts all --current-key-from-app secrag-backend`
  (read-only, any time); on a match, after the row-40 `pg_dump`, the same `rewrap` with
  `--read-write` on the tunnel; re-run the row-15 snippet → KO 0. No match: the owner deletes
  the account in the running pre-11a app before the promotion (`erase` refuses the tunnel).
- Then `shred`.

## Rollback

*Written before each promotion.* 11a: previous backend digest with the command overridden to
uvicorn only; the purge Job is kept; the 0005 downgrade is not a rollback path.

## Costs

*Measured the day after each promotion* (Cost Management query) and compared with the caps:
11a ≈ €0.20 (cap €1), 11b ≈ €0.15 (cap €1).
