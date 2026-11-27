#!/usr/bin/env bash
# Azure OpenAI gpt-4.1-mini vs gpt-5-mini live side-by-side check (14.7-migration-live, part 2b;
# --check-config / --prices added in part 2c).
#
#   bash scripts/azure/openai_live_check.sh --check-config [--prices <path>]
#   bash scripts/azure/openai_live_check.sh [--smoke-only] [--max-eur 0.30] [--prices <path>]
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
# --prices <path>: explicit override, passed straight through to the Python module (see
# resolve_prices_path()). Rarely needed -- the fallback chain above covers both the main repo
# clone and a worktree without it.
#
# What it does (full run, not --check-config):
#  1. Sources ~/.config/secrag/azure-openai.env (AZURE_OPENAI_ENDPOINT / _API_KEY / _DEPLOYMENT /
#     ...) into THIS shell's environment only. The values are never echoed, never printed, never
#     written to a log by this script -- only the Python module it calls
#     (backend/src/rag_app/eval/azure_side_by_side.py, built + unit-tested offline in this same
#     block) reads them, and that module's own output discipline never includes an endpoint or a
#     key either (see its module docstring).
#  2. Explicitly disables shell tracing (xtrace) for this whole script, including any inherited
#     SHELLOPTS/BASH_XTRACEFD that could otherwise turn it back on mid-script and print every
#     command (and therefore every sourced variable) to the terminal/log.
#  3. Runs `python -m rag_app.eval.azure_side_by_side` through the repo's normal backend venv
#     (same venv-resolution pattern as scripts/gate.sh: the shared venv under the git common dir,
#     one worktree up from wherever gate.sh/this script physically lives).
#  4. Prints a short human summary on screen and writes the full JSON + a markdown summary under
#     ~/secrag-agent-logs/14.7-migration-live/evidence/live-<UTC timestamp>/ -- no secrets, no
#     endpoint+key, no full prompts with user data: only ids, status codes, error types, token
#     usage, scores and EUR figures (see the Python module's own docstring for the same rule).
#
# Expected runtime: --smoke-only ~1-2 minutes (phases a+b only); the default full run (phases
# a+b+c, the 30-question side-by-side eval against BOTH deployments) ~5-10 minutes, mostly spent
# waiting on the two Azure OpenAI deployments.
#
# Expected cost: at most --max-eur (default 0.30 EUR, matching this block's PARALLEL_PLAN.md
# budget line) -- the Python module hard-stops BEFORE any call that would push the running total
# over that cap, and says so instead of making the call.
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

smoke_only=0
max_eur="0.30"
check_config=0
prices_path=""
while [ $# -gt 0 ]; do
  case "$1" in
    --smoke-only) smoke_only=1 ;;
    --max-eur) max_eur="${2:?--max-eur needs a value}"; shift ;;
    --check-config) check_config=1 ;;
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

echo "[openai_live_check] done (exit $status). Full evidence: $evidence_dir"
exit "$status"
