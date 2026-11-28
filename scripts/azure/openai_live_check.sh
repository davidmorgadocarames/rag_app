#!/usr/bin/env bash
# Azure OpenAI gpt-4.1-mini vs gpt-5-mini live side-by-side check (14.7-migration-live, part 2b;
# --check-config / --prices added in part 2c; --gate-db / --no-gate-db / --preflight-only added
# in part 2f).
#
#   bash scripts/azure/openai_live_check.sh --check-config [--prices <path>]
#   bash scripts/azure/openai_live_check.sh --preflight-only
#   bash scripts/azure/openai_live_check.sh [--smoke-only] [--max-eur 0.30] [--no-gate-db] [--prices <path>]
#
# RUN THIS YOURSELF, in your own Ubuntu/WSL terminal, from this worktree
# (~/proyectos/wt/14-A1 or wherever branch phase-14-assistant is checked out). NEVER from an
# agent session -- the permission system denies any agent command that references
# ~/.config/secrag/azure-openai.env at all, by design (F43, status/failures.md; the user's own
# secret boundary, memory "user-secret-handling"). This script is the ONE place that boundary is
# sanctioned to be crossed, because YOU are running it, not an agent.
#
# --check-config (part 2c, RUN THIS FIRST): resolves the azure_prices.json/golden-set paths and
# prints the gpt-4.1-mini/gpt-5-mini DataZone prices -- no secret env file read, no network call.
# Proves path resolution works from THIS worktree before anything that could cost money. A
# worktree's own status/ has no azure_prices.json (no status symlink, unlike the main repo clone)
# -- the Python module now falls back to $SECRAG_AZURE_PRICES, then the main repo clone's
# status/azure_prices.json, before failing closed with a clear message (see
# backend/src/rag_app/eval/azure_side_by_side.py's resolve_prices_path() docstring).
#
# --preflight-only (part 2f, RUN THIS NEXT -- still no secret env file, no Azure call): the
# FIRST real user run (2026-11-28 13:01) finished smoke+token-budget, then lost 13/30 and
# 18/30 side-by-side dialogues to OperationalError -- `docker ps` showed NO containers: phase
# (c)'s retrieval needs a Postgres with the seeded corpus, and DATABASE_URL had silently
# defaulted to the local dev database (nothing listening there either). This flag brings up
# the gate's own isolated DB (--gate-db, the default -- see below), restores the seed, runs
# ONLY the DB (reachable, chunks non-empty, embedding dimension matches) + Ollama (reachable,
# embedding dimension matches) checks the Python module's own `--preflight-only` exposes, then
# tears the stack down -- proving phase (c)'s prerequisites work on this machine BEFORE
# spending anything.
#
# --gate-db / --no-gate-db (part 2f): whenever phase (c) is going to run (i.e. not
# --smoke-only), this script needs SOME Postgres with the seeded corpus -- never the
# developer's real `rag_ia_pgdata` volume (CLAUDE.md "Data safety": experiments only run
# against the gate project or a throwaway). --gate-db (the DEFAULT -- this flag is a no-op,
# named only so the behaviour has a name in --help/this header) brings up the SAME isolated
# stack `scripts/gate.sh --full`/`--only eval` does: project `secrag-gate`, `compose.gate.yml`,
# port $GATE_DB_PORT (default 15432), roles via `scripts/db/apply_roles.sh`, `alembic upgrade
# head`, the gate seed for THIS tree's own current alembic head (restored via the SAME
# `scripts/gate.sh --resolve-seed-files <head>` lookup gate.sh's own seed-selection logic
# uses, F31 -- never a second, hand-rolled head-selection copy), and tears it down (`down -v`,
# this project only) on exit, always (a bash `trap ... EXIT`), success or failure. It takes
# the SAME global gate lock `scripts/gate.sh`/`scripts/dev/with-lock.sh` use
# (${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag/gate.lock) NON-BLOCKING: if a real `gate.sh` run (or
# another `--gate-db` run) already holds it, this refuses immediately rather than queueing
# behind it (a one-off ad-hoc check should never silently wait up to an hour). If the gate
# seed is missing or stale for this tree's alembic head, the error names the exact fix:
# `scripts/gate.sh --make-seed`. --no-gate-db opts out (e.g. to point at an already-running
# gate stack you brought up yourself) -- it REFUSES to start (rather than silently default to
# the local dev database) unless you also export DATABASE_URL yourself first.
#
# --prices <path>: explicit override, passed straight through to the Python module (see
# resolve_prices_path()). Rarely needed -- the fallback chain above covers both the main repo
# clone and a worktree without it.
#
# What it does (full run, not --check-config/--preflight-only):
#  1. If phase (c) is going to run, brings up the gate DB stack (--gate-db, the default --
#     see above) and exports its DATABASE_URL -- BEFORE the secret env file is sourced, so a
#     DB problem is caught and reported with zero secrets ever read.
#  2. Sources ~/.config/secrag/azure-openai.env (AZURE_OPENAI_ENDPOINT / _API_KEY / _DEPLOYMENT /
#     ...) into THIS shell's environment only. The values are never echoed, never printed, never
#     written to a log by this script -- only the Python module it calls
#     (backend/src/rag_app/eval/azure_side_by_side.py, built + unit-tested offline in this same
#     block) reads them, and that module's own output discipline never includes an endpoint or a
#     key either (see its module docstring).
#  3. Explicitly disables shell tracing (xtrace) for this whole script, including any inherited
#     SHELLOPTS/BASH_XTRACEFD that could otherwise turn it back on mid-script and print every
#     command (and therefore every sourced variable) to the terminal/log.
#  4. Runs `python -m rag_app.eval.azure_side_by_side` through the repo's normal backend venv
#     (same venv-resolution pattern as scripts/gate.sh: the shared venv under the git common dir,
#     one worktree up from wherever gate.sh/this script physically lives). The Python module's
#     own preflight (DA-14.7-migration-live-2f-1) checks the DB/Ollama again, right before phase
#     (a)'s first Azure call -- this script's own --gate-db bring-up and that in-process check
#     are complementary (this script provides the DB; the module proves it before spending).
#  5. Prints a short human summary on screen and writes the full JSON + a markdown summary under
#     ~/secrag-agent-logs/14.7-migration-live/evidence/live-<UTC timestamp>/ -- no secrets, no
#     endpoint+key, no full prompts with user data: only ids, status codes, error types, token
#     usage, scores and EUR figures (see the Python module's own docstring for the same rule).
#  6. Tears the gate DB stack down (if this script brought one up), always, on exit.
#
# Expected runtime: --smoke-only ~1-2 minutes (phases a+b only, no DB needed); --preflight-only
# ~1-2 minutes (gate DB up + seed restore + teardown, no Azure call); the default full run
# (phases a+b+c, the 30-question side-by-side eval against BOTH deployments) ~6-12 minutes
# (gate DB bring-up/seed/teardown adds roughly a minute either side of the ~5-10 minutes mostly
# spent waiting on the two Azure OpenAI deployments).
#
# Expected cost: at most --max-eur (default 0.30 EUR, matching this block's PARALLEL_PLAN.md
# budget line) -- the Python module hard-stops BEFORE any call that would push the running total
# over that cap, and says so instead of making the call. The preflight (DB/Ollama, step 1 above)
# runs before ANY Azure call, so a DB/Ollama problem costs EUR 0, always.
#
# Paste back to the agent/orchestrator: just the printed summary lines. There is nothing else to
# redact -- if you see anything that looks like your endpoint or key in the output, STOP, do not
# paste it, and report that as a bug in this script instead.

# --- xtrace off, defensively, before anything else ------------------------------------------
set +o xtrace 2>/dev/null || true
unset SHELLOPTS BASH_XTRACEFD 2>/dev/null || true

set -euo pipefail

SECRET_ENV_FILE="$HOME/.config/secrag/azure-openai.env"

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SELF")/../.." && pwd)"
cd "$REPO_ROOT"

GIT_COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$REPO_ROOT/.git")"
MAIN_ROOT="$(dirname "$GIT_COMMON")"

VENV=""
for candidate in "$MAIN_ROOT/backend/.venv" "$REPO_ROOT/backend/.venv"; do
  if [ -x "$candidate/bin/python" ]; then
    VENV="$candidate"
    break
  fi
done
if [ -z "$VENV" ]; then
  echo "ERROR: no backend venv found (checked $MAIN_ROOT/backend/.venv and $REPO_ROOT/backend/.venv)." >&2
  echo "  Build one first: cd backend && uv venv .venv && uv pip install -r requirements-torch.txt -r requirements-dev.txt" >&2
  exit 1
fi
PY="$VENV/bin/python"

# --- gate DB stack (part 2f): same isolated project/port/seed scripts/gate.sh uses -------------
#
# Delegates the parts that already exist as their OWN standalone, already-reviewed scripts/
# modules (never re-implemented here): role grants (scripts/db/apply_roles.sh), the isolation
# guard (rag_app.devtools.gate_compose), and the seed-file lookup for this tree's current
# alembic head (`scripts/gate.sh --resolve-seed-files <head>`, an existing re-entrant CLI entry
# point -- never a second, hand-rolled copy of gate.sh's own F31 head-selection logic). Only the
# bring-up/teardown SEQUENCING (compose up, roles, migrate, restore, compose down) is written
# here, mirroring scripts/gate.sh's own stack_up()/stack_down() at a glance, for a single `db`
# service brought up for the lifetime of ONE run of this script.
GATE_LOCK_FILE="${GATE_LOCK_FILE:-${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag/gate.lock}"
GATE_LOCK_HOLDER_FILE="$GATE_LOCK_FILE.holder"
GATE_DB_COMPOSE_PROJECT="secrag-gate"
GATE_DB_COMPOSE_FILE="$REPO_ROOT/compose.gate.yml"
GATE_DB_PORT="${GATE_DB_PORT:-15432}"
GATE_DB_USER="secrag_gate"
GATE_DB_NAME="secrag_gate"
GATE_STATE="$MAIN_ROOT/.gate"
GATE_ROLES_ENV="$GATE_STATE/roles.env"
GATE_DB_STACK_UP=0

gate_db_compose() {
  docker compose -p "$GATE_DB_COMPOSE_PROJECT" -f "$GATE_DB_COMPOSE_FILE" "$@"
}

gate_db_teardown() {
  if [ "$GATE_DB_STACK_UP" = 1 ]; then
    echo "[openai_live_check] gate-db: tearing down ($GATE_DB_COMPOSE_PROJECT only)" >&2
    GATE_DB_PASSWORD="${GATE_DB_PASSWORD:-unused}" gate_db_compose down -v --remove-orphans >/dev/null 2>&1 || true
    GATE_DB_STACK_UP=0
  fi
}

# Brings up the gate's own isolated DB, restores the seed for this tree's current alembic
# head, and exports DATABASE_URL for the Python module. Returns non-zero (nothing left
# running -- `gate_db_teardown` above is already wired into the EXIT trap by the caller
# before this function can fail) on any problem, with one clear, actionable message.
gate_db_up() {
  mkdir -p "$(dirname "$GATE_LOCK_FILE")" 2>/dev/null
  # NON-BLOCKING (T13.14.4's SAME lock file, never gate.sh's own 60-minute wait): a one-off
  # ad-hoc check must refuse immediately if a real gate.sh/with-lock.sh run already holds it,
  # never queue behind it.
  exec {GATE_DB_LOCK_FD}>"$GATE_LOCK_FILE" || {
    echo "ERROR: cannot open gate lock file $GATE_LOCK_FILE" >&2
    return 1
  }
  if ! flock -n "$GATE_DB_LOCK_FD"; then
    local holder
    holder="$(cat "$GATE_LOCK_HOLDER_FILE" 2>/dev/null)"
    echo "ERROR: the gate lock is held by ${holder:-another run} -- refusing to bring up a second gate DB stack (never waits). Wait for it to finish, or pass --no-gate-db with your own DATABASE_URL." >&2
    return 1
  fi
  printf 'block=openai_live_check branch=%s pid=%s mode=gate-db start=%s\n' \
    "$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)" "$$" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$GATE_LOCK_HOLDER_FILE" 2>/dev/null

  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker is not reachable from WSL -- start Docker Desktop first." >&2
    return 1
  fi

  GATE_DB_PASSWORD="$(openssl rand -hex 16)"
  export GATE_DB_PASSWORD GATE_DB_PORT
  local config
  config="$(gate_db_compose config --format json)" || {
    echo "ERROR: compose.gate.yml does not resolve" >&2
    return 1
  }
  if ! echo "$config" | PYTHONPATH="$REPO_ROOT/backend/src" "$PY" -m rag_app.devtools.gate_compose; then
    echo "ERROR: compose.gate.yml is not isolated from the development stack (see above)" >&2
    return 1
  fi
  gate_db_compose down -v --remove-orphans >/dev/null 2>&1 || true   # stale volume from a killed run
  GATE_DB_STACK_UP=1
  echo "[openai_live_check] gate-db: starting the gate's own isolated DB (project $GATE_DB_COMPOSE_PROJECT, port $GATE_DB_PORT)" >&2
  if ! gate_db_compose up -d --wait db; then
    echo "ERROR: gate DB did not become healthy" >&2
    return 1
  fi

  mkdir -p "$GATE_STATE" 2>/dev/null
  chmod 700 "$GATE_STATE" 2>/dev/null || true
  if [ ! -f "$GATE_ROLES_ENV" ]; then
    (
      umask 077
      {
        echo "# Local passwords for db/roles.sql roles (scripts/db/apply_roles.sh). Git-ignored."
        echo "SECRAG_PURGER_PASSWORD=$(openssl rand -hex 24)"
        echo "SECRAG_BACKUP_PASSWORD=$(openssl rand -hex 24)"
      } >"$GATE_ROLES_ENV.tmp" && mv -f "$GATE_ROLES_ENV.tmp" "$GATE_ROLES_ENV"
    )
  fi
  if ! command -v psql >/dev/null; then
    echo "ERROR: psql not found (scripts/prereqs/install.sh pg) -- needed to apply db/roles.sql" >&2
    return 1
  fi
  if ! (
    set -a
    # shellcheck disable=SC1090
    . "$GATE_ROLES_ENV"
    set +a
    PGPASSWORD="$GATE_DB_PASSWORD" bash "$REPO_ROOT/scripts/db/apply_roles.sh" \
      "postgresql://$GATE_DB_USER@127.0.0.1:$GATE_DB_PORT/$GATE_DB_NAME"
  ); then
    echo "ERROR: db/roles.sql failed on the gate DB" >&2
    return 1
  fi

  local gate_database_url
  gate_database_url="postgresql+psycopg://$GATE_DB_USER:$GATE_DB_PASSWORD@127.0.0.1:$GATE_DB_PORT/$GATE_DB_NAME"
  if ! (cd "$REPO_ROOT/backend" && DATABASE_URL="$gate_database_url" "$VENV/bin/alembic" upgrade head); then
    echo "ERROR: alembic upgrade head failed on the gate DB" >&2
    return 1
  fi

  local current_head seed_resolved seed_file
  current_head="$(gate_db_compose exec -T db psql -U "$GATE_DB_USER" -d "$GATE_DB_NAME" \
    -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')"
  if [ -z "$current_head" ]; then
    echo "ERROR: could not read the gate DB's own alembic_version after migrating" >&2
    return 1
  fi
  if ! seed_resolved="$(bash "$REPO_ROOT/scripts/gate.sh" --resolve-seed-files "$current_head")"; then
    echo "ERROR: ${seed_resolved#SEED_ERROR=}" >&2
    return 1
  fi
  seed_file="$(printf '%s\n' "$seed_resolved" | sed -n 's/^SEED_FILE=//p')"
  if [ -z "$seed_file" ] || [ ! -f "$seed_file" ]; then
    echo "ERROR: no gate seed for alembic head $current_head -- run: scripts/gate.sh --make-seed" >&2
    return 1
  fi
  echo "[openai_live_check] gate-db: restoring seed from $seed_file" >&2
  if ! gate_db_compose exec -T db pg_restore -U "$GATE_DB_USER" -d "$GATE_DB_NAME" \
    --data-only --disable-triggers --no-owner <"$seed_file"; then
    echo "ERROR: seed restore failed" >&2
    return 1
  fi

  export DATABASE_URL="$gate_database_url"
  echo "[openai_live_check] gate-db: ready on 127.0.0.1:$GATE_DB_PORT (project $GATE_DB_COMPOSE_PROJECT)" >&2
}

smoke_only=0
max_eur="0.30"
check_config=0
preflight_only=0
no_gate_db=0
prices_path=""
while [ $# -gt 0 ]; do
  case "$1" in
    --smoke-only) smoke_only=1 ;;
    --max-eur) max_eur="${2:?--max-eur needs a value}"; shift ;;
    --check-config) check_config=1 ;;
    --preflight-only) preflight_only=1 ;;
    --gate-db) : ;;   # the default -- accepted as a no-op so the behaviour has an explicit name
    --no-gate-db) no_gate_db=1 ;;
    --prices) prices_path="${2:?--prices needs a value}"; shift ;;
    -h|--help) sed -n '2,50p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

prices_args=()
if [ -n "$prices_path" ]; then
  prices_args=(--prices "$prices_path")
fi

# --check-config needs none of the secret-env-file machinery below: no sourcing, no evidence
# dir, no network call -- just path resolution + a local JSON read, run through the same venv.
if [ "$check_config" = 1 ]; then
  echo "[openai_live_check] --check-config: resolving paths only, no env file, no network call"
  status=0
  (cd "$REPO_ROOT/backend" && PYTHONPATH="$REPO_ROOT/backend/src" "$PY" \
    -m rag_app.eval.azure_side_by_side --check-config "${prices_args[@]}") || status=$?
  exit "$status"
fi

# Phase (c) (and --preflight-only, which checks exactly phase (c)'s own prerequisites) need a
# Postgres with the seeded corpus; --smoke-only never reaches phase (c), so it never needs one.
needs_db=0
if [ "$preflight_only" = 1 ] || [ "$smoke_only" = 0 ]; then
  needs_db=1
fi

if [ "$needs_db" = 1 ]; then
  if [ "$no_gate_db" = 1 ]; then
    if [ -z "${DATABASE_URL:-}" ]; then
      echo "ERROR: --no-gate-db was passed but DATABASE_URL is not set -- refusing to silently" >&2
      echo "  fall back to the local development database (never used for an experiment; see" >&2
      echo "  CLAUDE.md 'Data safety'). Export DATABASE_URL yourself first (pointing at an" >&2
      echo "  already-running gate stack, or a throwaway of your own), or drop --no-gate-db to" >&2
      echo "  use this script's own isolated gate DB stack." >&2
      exit 1
    fi
    echo "[openai_live_check] --no-gate-db: using the DATABASE_URL already set in this shell" >&2
  else
    trap 'gate_db_teardown' EXIT
    if ! gate_db_up; then
      exit 1
    fi
  fi
fi

if [ "$preflight_only" = 1 ]; then
  echo "[openai_live_check] --preflight-only: DB/Ollama checks only, no env file, no Azure call"
  status=0
  (cd "$REPO_ROOT/backend" && PYTHONPATH="$REPO_ROOT/backend/src" "$PY" \
    -m rag_app.eval.azure_side_by_side --preflight-only "${prices_args[@]}") || status=$?
  gate_db_teardown   # also run via the EXIT trap -- idempotent, explicit for clarity/shellcheck
  exit "$status"
fi

if [ ! -f "$SECRET_ENV_FILE" ]; then
  echo "ERROR: $SECRET_ENV_FILE not found." >&2
  echo "  Create it yourself first (see memory 'user-secret-handling') -- this script never" >&2
  echo "  creates or prompts for it." >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
evidence_dir="$HOME/secrag-agent-logs/14.7-migration-live/evidence/live-$timestamp"
mkdir -p "$evidence_dir"

echo "[openai_live_check] loading Azure OpenAI credentials from $SECRET_ENV_FILE (values never echoed)"
set -a
# shellcheck disable=SC1090
. "$SECRET_ENV_FILE"
set +a

args=(-m rag_app.eval.azure_side_by_side --max-eur "$max_eur" --output-dir "$evidence_dir")
if [ "$smoke_only" = 1 ]; then
  args+=(--smoke-only)
fi
args+=("${prices_args[@]}")

echo "[openai_live_check] running (this talks to Azure OpenAI; cap --max-eur=$max_eur; evidence -> $evidence_dir)"
# Same convention as scripts/gate.sh: run from backend/ with backend/src on PYTHONPATH (this
# repo's package is not pip-installed into the venv, so `python -m rag_app...` needs both).
status=0
(cd "$REPO_ROOT/backend" && PYTHONPATH="$REPO_ROOT/backend/src" "$PY" "${args[@]}") || status=$?

gate_db_teardown   # also run via the EXIT trap -- idempotent, explicit for clarity/shellcheck
echo "[openai_live_check] done (exit $status). Full evidence: $evidence_dir"
exit "$status"
