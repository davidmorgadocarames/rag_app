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
were already stopped (`rag_ia-db-1` exited 2026-09-24, `rag_app-db-1` exited 2026-09-27), so a
file-level copy is consistent. Each volume was copied read-only into a git-ignored, mode-700
directory outside the repository, the copy restored into a new throw-away volume, and a
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
  (`default_transaction_read_only=on`; `--read-write` is explicit). The password comes from
  the backend's `database-url` secret into a 0600 `PGPASSFILE`; it never reaches argv or the
  output. The rule is **always** removed (EXIT trap, including SIGINT/SIGTERM/SIGHUP, with
  signals ignored during the removal), and then verified gone; otherwise the script prints the
  manual command and fails. Tested against a stateful fake `az`/`curl`/`psql`
  (`backend/tests/test_db_tunnel.py`, 22 cases: rule present only during the call; removed on
  success, error, SIGINT and SIGTERM; sweep; `--sweep`; seven unknown-IP cases; read-only;
  password never printed; the delete-failure path). Mutations (INT trap, sweep, read-only
  removed) fail the tests.
- **`scripts/azure/sql/diag_counts.sql`** — the counts above, as `name|value` lines.
- **`scripts/azure/diag_keys.py`** — runs inside the backend and reads settings and the DB as
  the app does, in a read-only transaction. It prints exactly five lines (`N`, `OK`, `KO`,
  `JWT_SECRET length >= 32: OK|KO`, `DATA_MASTER_KEY valid Fernet: OK|KO`); a failure prints
  the exception class only. `scripts/azure/exec_oneliner.py` wraps it into one
  whitespace-free `python -c …` argument for `az containerapp exec --command`. It was verified
  in the backend image `rag_app-backend:bc7c661` (same Dockerfile as the deployed `dee9cbc`)
  in four ways: as argv, through `sh -c` (`--for shell`), over stdin (`python -`), and with a
  wrong key (KO = N).

### Azure (T11.1.3) — *to be filled in by the orchestrator*

| Item | Result |
|---|---|
| Spike: stdin (`python -`) through `exec` / `--command` split on whitespace or by a shell | *pending* |
| db-tunnel counts: `users` / `user_keys` / users without a key / `conversations` / `messages` / `alembic_version` | *pending* |
| Key snippet: N / OK / KO | *pending* |
| `JWT_SECRET` length ≥ 32 / `DATA_MASTER_KEY` valid Fernet | *pending* |
| Firewall: no `secrag-tunnel-*` rule left after the runs | *pending* |
| **Classification** (a / b / c) and consequence (R5-5 in row 40 if b) | *pending* |

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
