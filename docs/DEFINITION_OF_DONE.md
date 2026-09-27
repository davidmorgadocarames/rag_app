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
| 2 | No secrets committed: `.env` files git-ignored, gitleaks clean | `secrets`, `gitleaks` |
| 3 | No hardcoding of config — everything via `pydantic-settings` | review |
| 4 | Dependencies pinned | `pinned-deps` (`==` in every requirements file) |
| 5 | Lint + format clean (`ruff check`, `ruff format --check`) | `ruff-lint`, `ruff-format` |
| 6 | Types clean (`mypy` strict) | `mypy` |
| 7 | Tests green (`pytest`); new logic has tests; DB tests run through the **test DB harness** | `pytest` (unit), `db-tests` (`--full` / CI) |
| 8 | Frontend touched → `eslint` + `tsc --noEmit` + `next build`, with Node 22 (≥ 22.13) and npm ≥ 11 (`frontend/package.json` `engines`; installed in `~/.local/bin` by `scripts/prereqs/install.sh`, which the gate puts first on `PATH`) | `frontend` (checks the versions it runs with — wrong toolchain fails) |
| 9 | **Verified end-to-end** — actually exercised, not only unit tests | judgment (checklist) |
| 10 | Docs updated (README roadmap + relevant docs); relative links resolve | judgment + `adr-links` |
| 11 | CI green after push; conventional commit message | CI + review |
| 12 | Shell scripts clean | `shellcheck` (every tracked `*.sh` and `.githooks/*`) |
| 13 | No known vulnerable dependency without a dated, justified exception | `dependency-audit` |
| 14 | Evaluation data well-formed (golden set, judge labels, thresholds, baseline) | `schema-check` |

## Phase-specific norms

- **Phase 4+ (evaluation)** — the **eval gate**: retrieval metrics (context recall/precision),
  generation metrics (faithfulness, answer relevance), and **correctness vs ground truth** meet
  their thresholds; results do not regress below `baseline_metrics.json`; the LLM-judge is
  validated against human labels; correct-abstention rate on the negative set meets its threshold.
  Enforced by the gate step `eval` (in `--full`).
- **Phase 10 (cloud)** — the deployed Azure URL responds to a health check; the eval gate
  ran and passed **before** the Azure deploy step (not just before the GHCR push) — the
  pre-push gate blocks the very push that triggers CD, and the deploy job is
  `needs: [images]`; Azure credentials/secrets are supplied via OIDC federated credentials
  and Container Apps secrets, never committed (this extends norm #2, it is not a new
  mechanism). Note: norm #1 (**WSL2**) governs where the code is *written and tested* — it
  does **not** conflict with a container that *runs in production* on Azure.
- **Phase 11+ (gate hygiene, X5)** — `--full` runs only in the isolated gate project; every
  sub-phase closes with `--full` or `--only <the steps it adds>`; target `--full` ≤ 30 min.

## Gate modes

| Mode | When | What |
|---|---|---|
| `--fast` (default) | pre-push on phase branches | every step marked `-` below; no stack needed |
| `--full` | pre-push to `main` (promotion), before a PR is ready | fast + stack steps in the gate project: up → migrate → seed → steps → `down -v`. A missing tool or an unreachable stack **fails** (never skips) |
| `--only a,b` | closing a sub-phase, debugging | just the named steps (the gate project is started only if one needs it) |
| `--make-seed` | once, and whenever `data/chunks/chunks.jsonl` changes | builds the git-ignored seed `.gate/seed.dump` (documents + chunks, embedded with Ollama) in the gate project |
| `--list` | — | step names, stack needs and budgets |

Steps and time budgets (a step that exceeds its budget is killed and fails):

| Step | Stack | Budget | Step | Stack | Budget |
|---|---|---|---|---|---|
| `environment` | - | 10 s | `gitleaks` | - | 180 s |
| `secrets` | - | 10 s | `shellcheck` | - | 60 s |
| `pinned-deps` | - | 10 s | `schema-check` | - | 30 s |
| `venv` | - | 10 s | `adr-links` | - | 30 s |
| `ruff-lint` | - | 60 s | `frontend` | - | 600 s |
| `ruff-format` | - | 60 s | `dependency-audit` | - | 240 s |
| `mypy` | - | 240 s | `db-tests` | gate DB | 300 s |
| `pytest` | - | 300 s | `eval` | gate DB + seed + Ollama | 1200 s |

Gate-project setup (up + migrate + seed restore) has its own 300 s budget. Measured on the
development machine (2026-09-27): `--fast` ≈ 26 s, `--full` ≈ 3 min (eval ≈ 2.5 min).

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
marker comment, refuses any database without that marker, migrates it (`alembic upgrade
head`), applies `db/roles.sql` when present and drops it at the end. Without
`TEST_DATABASE_URL` the `db` tests are skipped — unless `SECRAG_REQUIRE_DB_TESTS=1`
(`--full`, CI), where that is an error.

**Dependency audit.** `pip-audit` over the backend venv and `npm audit --omit=dev` over the
frontend. Every advisory must match an entry of `audit-exceptions.toml` (advisory, package,
ecosystem, reason, `expires` date); an expired entry fails even if the advisory is gone.

**Links.** `adr-links` checks every relative link and heading anchor in tracked Markdown;
with `RUNBOOK_PATH=<private runbook>` it also checks the runbook (unset → explicit SKIP).

**Pre-push hook.** `.githooks/pre-push` reads the pushed refs from stdin: only deletions →
exit 0; any ref to `refs/heads/main` → `--full`, otherwise `--fast`. It runs the gate from a
temporary worktree at each pushed SHA (the backend venv, `.gate/` seed and `data/` come from
the main tree, `.env` files are linked from it, the frontend gets `npm ci`).

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
