# Definition of Done — SecRAG phase gate

Every phase must satisfy **all** applicable norms below before it is considered done and
before its work is pushed. The mechanical norms are enforced by `scripts/gate.sh` (the
`git push` hook runs it automatically — see [Gate modes](#gate-modes)). The judgment norms
are a human/agent checklist.

> This exists because "it works on Windows for now" once slipped through: the gate makes the
> project's norms a barrier, not a good intention. The environment (WSL2) is only **one** norm.

## Universal norms (every phase)

| # | Norm | Enforced by (gate step) |
|---|------|-------------|
| 1 | Developed and run in **WSL2 Ubuntu** (GPU used where relevant) | `environment` (`/proc/version`) |
| 2 | No secrets committed: `.env` files git-ignored, gitleaks clean | `secrets`, `gitleaks` (committed history + staged changes, same gitleaks version as CI; reviewed false positives in `.gitleaksignore`) |
| 3 | No hardcoding of config — everything via `pydantic-settings` | review |
| 4 | Dependencies pinned, and the gate runs with exactly those pins | `pinned-deps` (`==` in every requirements file), `venv` (installed == pins) |
| 5 | Lint + format clean (`ruff check`, `ruff format --check`) | `ruff-lint`, `ruff-format` |
| 6 | Types clean (`mypy` strict) | `mypy` |
| 7 | Tests green (`pytest`); new logic has tests; DB tests run through the **test DB harness** | `pytest` (unit), `db-tests` (`--full` / CI) |
| 8 | Frontend touched → `eslint` + `tsc --noEmit` + `next build`, with Node 22 (≥ 22.13) and npm ≥ 11 (`frontend/package.json` `engines`; installed in `~/.local/bin` by `scripts/prereqs/install.sh`, which the gate puts first on `PATH`) | `frontend` (checks the versions it runs with — wrong toolchain fails) |
| 9 | **Verified end-to-end** — actually exercised, not only unit tests | judgment (checklist) |
| 10 | Docs updated (README roadmap + relevant docs); relative links resolve | judgment + `adr-links` |
| 11 | CI green after push; conventional commit message | CI + review |
| 12 | Shell scripts clean | `shellcheck` (every tracked `*.sh`, `.githooks/*` and sh/bash-shebang file) |
| 13 | No known vulnerable dependency without a dated, justified exception | `dependency-audit` |
| 14 | Evaluation data well-formed (golden set, judge labels, thresholds, baseline) | `schema-check` |
| 15 | The gate cannot be skipped silently: tracked scripts `100755` in git and executable on disk; the pushing clone has `core.hooksPath = .githooks` and an executable `.githooks/pre-push` | `git-modes` (+ `scripts/prereqs/check.sh`; CI job `cheap-checks` for the modes) |

## Phase-specific norms

- **Phase 4+ (evaluation)** — the **eval gate**: retrieval metrics (context recall/precision),
  generation metrics (faithfulness, answer relevance), and **correctness vs ground truth** meet
  their thresholds; results do not regress below `baseline_metrics.json`; the LLM-judge is
  validated against human labels; correct-abstention rate on the negative set meets its threshold.
  Enforced by the gate step `eval` (in `--full`).
- **Phase 10 (cloud)** — the deployed Azure URL responds to a health check; the eval gate
  ran and passed **before** the Azure deploy step (not just before the GHCR push) — the
  pre-push gate blocks the very push that triggers CD, and (from Phase 11a) CD runs only
  after CI succeeded on `main` and deploys only a SHA carrying `secrag/gate-full` (below);
  Azure credentials/secrets are supplied via OIDC federated credentials
  and Container Apps secrets, never committed (this extends norm #2, it is not a new
  mechanism). Note: norm #1 (**WSL2**) governs where the code is *written and tested* — it
  does **not** conflict with a container that *runs in production* on Azure.
- **Phase 11+ (gate hygiene, X5)** — `--full` runs only in the isolated gate project; every
  sub-phase closes with `--full` or `--only <the steps it adds>`; target `--full` ≤ 30 min.
- **Promotion evidence (Phase 11+, D-2026-09-27-7 a)** — the pre-push hook can be bypassed
  (`--no-verify`, a lost exec bit, an unset `core.hooksPath`), so a promotion to `main`
  (PHASE_TASKS rows 40/41) needs the **`--full` PASS log for the exact SHA pushed**: the
  gate's summary prints `total … @ <full SHA>` and `GATE: PASS`; the log is attached to the
  phase report / PR. CI's `cheap-checks` job is the server-side backstop for the cheap steps
  (git modes, shellcheck, venv, dependency-audit, adr-links).
- **Gate status enforced by CD (Phase 11+, D-2026-09-27-7 b)** — a `--full` PASS on a clean
  tree publishes the GitHub commit status **`secrag/gate-full` = success for exactly that
  SHA** (`scripts/cd/gate_status.sh`, via `gh`). CD's `gate-status` job and the `deploy` job
  itself refuse a SHA whose newest `secrag/gate-full` status is missing, not `success`, or
  not posted by the repository owner (`vars.GATE_STATUS_CREATOR` overrides). Under the
  pre-push hook the commit is not on GitHub yet, so a detached publisher posts the status
  once the push lands (log: `.git/secrag-gate/publish-status.log`); CD waits up to 10 min
  for it. A dirty tree (at start or end), a HEAD that moved during the run, a missing `gh`
  or `SECRAG_GATE_PUBLISH=0` → no status (said in the
  gate output) → CD refuses. By hand, after a `--full` PASS on a pushed SHA:
  `bash scripts/cd/gate_status.sh publish <sha>`. A status can still be posted without
  running the gate — that is a deliberate act, not an accident (accepted in D-7).

## Gate modes

| Mode | When | What |
|---|---|---|
| `--fast` (default) | pre-push on phase branches | every step marked `-` below; no stack needed |
| `--full` | pre-push to `main` (promotion), before a PR is ready | fast + stack steps in the gate project: up → roles → migrate → seed → steps → `down -v`. A missing tool or an unreachable stack **fails** (never skips). A PASS on a clean tree publishes `secrag/gate-full` for the SHA (see above) |
| `--only a,b` | closing a sub-phase, debugging, CI `cheap-checks` | just the named steps (the gate project is started only if one needs it). A missing tool **fails**, as in `--full` — a step that was asked for never passes without running |
| `--make-seed` | once, and whenever the corpus (`data/chunks/chunks.jsonl`), the embedding model build (Ollama digest) or the migrations head changes | builds the git-ignored seed `.gate/seed.dump` (documents + chunks, embedded with Ollama) in the gate project; `seed.meta` records corpus hash, `embed_model` digest and `alembic_head`, and `eval` fails on any mismatch |
| `--list` | — | step names, stack needs and budgets |

Steps and time budgets (a step that exceeds its budget is killed and fails):

| Step | Stack | Budget | Step | Stack | Budget |
|---|---|---|---|---|---|
| `environment` | - | 10 s | `gitleaks` | - | 180 s |
| `secrets` | - | 10 s | `shellcheck` | - | 60 s |
| `git-modes` | - | 10 s | `schema-check` | - | 30 s |
| `pinned-deps` | - | 10 s | `adr-links` | - | 30 s |
| `venv` | - | 10 s | `frontend` | - | 600 s |
| `ruff-lint` | - | 60 s | `dependency-audit` | - | 240 s |
| `ruff-format` | - | 60 s | `db-tests` | gate DB | 300 s |
| `mypy` | - | 240 s | `eval` | gate DB + seed + Ollama | 1200 s |
| `pytest` | - | 300 s | `migrations-roundtrip` | gate DB | 180 s |

`migrations-roundtrip` (T11.0.13; the CI leg and the broken-downgrade test come with
T11.2.9): a fresh database on the gate server → `db/roles.sql` → `alembic upgrade head` →
`pg_dump` as `secrag_backup` (only the default privileges can make the new tables readable)
→ `roles.sql` again with the catalog compared (roles, grants, default privileges, every
table/sequence ACL: identical) → `secrag_purger` / `secrag_backup` log in with the gate
passwords → `downgrade -1` → `upgrade head`. The local role passwords live in the
git-ignored `.gate/roles.env` (0600, generated once).

Gate-project setup (up + roles + migrate + seed restore) has its own 300 s budget; building a
cached venv (below) has 900 s. Measured on the development machine (2026-09-27): `--fast`
≈ 26 s, `--full` ≈ 3 min (eval ≈ 2.5 min).

**Venv matching the pins** (DA-B-4). The gate resolves the backend venv before any step: the
main tree's `backend/.venv` when the checked tree's requirements hash equals the main tree's
and the installed versions equal the pins; otherwise (a pushed commit that changes the pins)
a venv cached per requirements hash under `$(git rev-parse --git-common-dir)/secrag-gate/venvs/`
(built once with `uv`: same Python and CPU torch as the main venv, then
`requirements-dev.txt`; the two most recent are kept). In the main tree a drifted venv is
never rebuilt behind the developer's back: the gate fails (every mode, also `--only <step>`
without the `venv` step — DA-B2-2) with the install command.
`GATE_VENV=<dir>` overrides the choice (CI); the `venv` step still checks it.

**Isolation.** `compose.gate.yml` is self-contained: project `secrag-gate`, volume
`secrag_gate_pgdata`, database published on `127.0.0.1:${GATE_DB_PORT:-15432}` only (not
55432: Windows reserves blocks of its dynamic port range for Hyper-V). Before every `up`
the gate checks the resolved configuration (`rag_app.devtools.gate_compose`): project name,
`secrag_gate_*` volumes only, no bind mounts, loopback-only ports, none of the development
ports (5432, 8000, 3000, 11434). Every native step gets the gate's `DATABASE_URL` and
`OLLAMA_HOST` exported (in `--fast` the URL points at the stopped gate port, so nothing can
reach the development database). `OLLAMA_HOST` = `GATE_OLLAMA_HOST`, else `OLLAMA_HOST`,
else the main tree's `.env`, else `http://127.0.0.1:11434` (native `ollama serve`).
Teardown is `docker compose -p secrag-gate down -v` — it only removes that project.

**Test DB harness** (`backend/tests/db_harness.py`, marker `db`). `TEST_DATABASE_URL` is a
maintenance connection on a local test server; the harness refuses port 5432, database
`rag` and any non-loopback host before connecting, creates `secrag_test_<random>` with a
marker comment, refuses any database without that marker, applies `db/roles.sql` when
present, migrates it (`alembic upgrade head`) and drops it at the end. Without
`TEST_DATABASE_URL` the `db` tests are skipped — unless `SECRAG_REQUIRE_DB_TESTS=1`
(`--full`, CI), where that is an error. While the harness database exists, the app's own
`DATABASE_URL` points at it (whatever the shell or `backend/.env` say), the app's cached
session factories are reset around each `db` test, and a `db` test aborts the run (rc 4) if
app code (`get_settings()`, `make_engine()`, the API) would resolve anything but a
`secrag_test_*` database on a loopback, non-5432 port. `alembic.ini` keeps `sqlalchemy.url`
empty (tested), so Alembic always follows `DATABASE_URL`.

**Dependency audit.** `pip-audit` over the backend venv and `npm audit --omit=dev` over the
frontend. Every advisory must match an entry of `audit-exceptions.toml` (advisory, package,
ecosystem, reason, `expires` date); an expired entry fails even if the advisory is gone, and
a duplicated entry is invalid. Each reason states how (or whether) the vulnerable code is
reachable in SecRAG; `INTERIM` entries expire with the block that fixes them, `PERMANENT`
ones (no fixed version) carry a re-review date. A package pip-audit cannot find on PyPI
because of a local version label (`torch 2.14.0+cpu` from the PyTorch CPU index) is audited
by its base version through the OSV API; any other unauditable package fails. CI runs the
same step in the `cheap-checks` job.

**Links.** `adr-links` checks every relative link and heading anchor in tracked Markdown;
with `RUNBOOK_PATH=<private runbook>` it also checks the runbook (unset → explicit SKIP).

**Pre-push hook.** `.githooks/pre-push` reads the pushed refs from stdin: only deletions →
exit 0; any ref to `refs/heads/main` → `--full`, otherwise `--fast`. It runs the gate from a
temporary worktree at each pushed SHA (`.gate/` seed and `data/` come from the main tree,
`.env` files are linked from it, the backend venv matches the pushed pins — see above — and
the frontend gets `npm ci`). `SECRAG_PREPUSH_DRY_RUN=1` (tests only) prints the plan and
exits **10**, so a forgotten export blocks the push instead of skipping the gate. After
editing files from Windows through `\\wsl.localhost`, check `git diff --summary` for mode
changes before committing (`git-modes` and `check.sh` catch a hook that lost its exec bit).

## How to run

```bash
# inside WSL2, from the repo root
bash scripts/prereqs/install.sh       # one-time: user-level toolchain (Node/npm, pg16 client, age, …)
bash scripts/prereqs/check.sh         # prerequisites: every item OK / KO / PENDING
git config core.hooksPath .githooks   # one-time: enable the pre-push gate for this clone
bash scripts/gate.sh                  # --fast: all stack-free norms; non-zero exit = blocked
bash scripts/gate.sh --make-seed      # once: seed for the gate project (needs Docker + Ollama)
bash scripts/gate.sh --full           # everything, in the isolated gate project
```

Judgment norms (#9, #10) are confirmed by the author/agent before marking a phase done.
