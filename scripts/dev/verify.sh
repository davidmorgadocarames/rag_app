#!/usr/bin/env bash
# scripts/dev/verify.sh <feature-id> [--base-url URL]
#
# Self-verification for ONE feature from the generated docs/FEATURE_MAP.md (T13.17.3):
# resolves which `frontend/e2e/*.spec.ts` file(s) cover <feature-id> (via `python -m
# rag_app.features info <feature-id>`, the same table the map is generated from -- never a
# second, independently-maintained list), runs them with Playwright against a real, running
# app, and writes evidence to a folder it prints on its last line (`evidence: <dir>`).
#
# Usage:
#   scripts/dev/verify.sh <feature-id>                  bring up what the feature needs
#                                                        locally and verify against it
#   scripts/dev/verify.sh <feature-id> --base-url URL   verify against an ALREADY RUNNING
#                                                        app at URL (local dev stack, the
#                                                        gate's own stack, an Azure URL) --
#                                                        never brings anything up; BLOCKED if
#                                                        URL is not reachable
#   ... --base-url URL --allow-remote-writes            required in addition to --base-url
#                                                        for any feature whose covering
#                                                        spec(s) are marked "mutating" in
#                                                        docs/FEATURE_MAP.md (account
#                                                        creation, e-mail sending, erasure,
#                                                        lab runs...) -- see the DA-HB-2
#                                                        mutation gate below. Without this
#                                                        flag, a mutating feature against
#                                                        --base-url is BLOCKED, never run.
#
# Exit codes:
#   0  PASS      the covering spec(s) ran and passed
#   1  FAIL      the covering spec(s) ran and at least one failed
#   2  BLOCKED   unknown feature id, no covering e2e spec (see docs/FEATURE_MAP.md's
#                coverage table -- use `python -m rag_app.devtools.registry_check` for a
#                probe-only feature instead), or a missing prerequisite (e.g. nothing
#                reachable at --base-url)
#
# Locking (T13.17.3's "reuse scripts/dev/with-lock.sh", Paralelo-B problem #4): the
# `--base-url` path takes the SAME global gate lock scripts/gate.sh uses (this script's own
# Playwright run still shares the native Ollama/ports with a concurrent gate run even when
# the APP under test is remote). The no-`--base-url` (local bring-up) path instead
# DELEGATES ENTIRELY to `scripts/gate.sh --only e2e` for the actual stack bring-up/seed/
# teardown -- the existing, already-reviewed machinery for exactly this (throwaway
# `secrag-gate` compose project, generated DB password, seeded admin/MFA accounts, Mailpit;
# never the developer's real `rag_ia_pgdata` volume) -- rather than a second, hand-rolled,
# secrets-handling copy of it here. `gate.sh` already takes the SAME lock file internally
# (confirmed: both default to `${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag/gate.lock`), so this
# script does NOT ALSO wrap that call in with-lock.sh -- doing so would flock the same file
# from the same process tree twice and self-deadlock. The trade-off: `gate.sh --only e2e`
# runs its whole fixed e2e bundle (every spec, not just this feature's), so this path is
# coarser-grained and slower than --base-url against an already-running app.
set -uo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SELF")/../.." && pwd)" || exit 1
cd "$REPO_ROOT" || exit 1

usage() {
  echo "usage: $(basename "$0") <feature-id> [--base-url URL [--allow-remote-writes]]" >&2
  echo "       (feature ids: see docs/FEATURE_MAP.md, or 'python -m rag_app.features map')" >&2
  exit 2
}

FEATURE_ID="${1:-}"
[ -n "$FEATURE_ID" ] || usage
shift
BASE_URL=""
# DA-HB-2: a feature whose covering spec(s) create/change real remote state (account
# creation, e-mail sending, erasure, lab runs...) must never run against a remote
# --base-url without the operator explicitly accepting that -- see the mutation gate below.
ALLOW_REMOTE_WRITES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base-url)
      [ $# -ge 2 ] || usage
      BASE_URL="$2"
      shift 2
      ;;
    --allow-remote-writes)
      ALLOW_REMOTE_WRITES=1
      shift
      ;;
    -h | --help) usage ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      ;;
  esac
done

for tool in jq curl npx; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "BLOCKED: required tool '$tool' not found on PATH" >&2
    exit 2
  }
done

# Same venv resolution as scripts/gate.sh (DA-B-4): a worktree (Paralelo-B, T13.14.1) shares
# the main tree's venv rather than duplicating it.
GIT_COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$REPO_ROOT/.git")"
MAIN_ROOT="$(dirname "$GIT_COMMON")"
VENV="${GATE_VENV:-$MAIN_ROOT/backend/.venv}"
PY="$VENV/bin/python"
[ -x "$PY" ] || {
  echo "BLOCKED: backend venv not found at $VENV (run 'scripts/gate.sh --only environment' once)" >&2
  exit 2
}

EVIDENCE_ROOT="${SECRAG_VERIFY_EVIDENCE_DIR:-$HOME/secrag-verify-evidence}"
EVIDENCE_DIR="$EVIDENCE_ROOT/$FEATURE_ID/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE_DIR" || {
  echo "BLOCKED: cannot create evidence dir $EVIDENCE_DIR" >&2
  exit 2
}

blocked() {
  # DA-HB-1: use the FULL message ($*, every argument joined), never just $1 -- a call site
  # passing its reason as several separate arguments (readability) must not get silently
  # truncated to the first one in the evidence this tool exists to produce.
  local msg="$*"
  echo "BLOCKED: $msg" >&2
  printf '%s\n' "BLOCKED: $msg" >"$EVIDENCE_DIR/verdict.log"
  echo "evidence: $EVIDENCE_DIR"
  exit 2
}

INFO_JSON="$(PYTHONPATH="$REPO_ROOT/backend/src" "$PY" -m rag_app.features info "$FEATURE_ID" 2>"$EVIDENCE_DIR/info-stderr.log")"
echo "$INFO_JSON" >"$EVIDENCE_DIR/feature-info.json"

FOUND="$(echo "$INFO_JSON" | jq -r '.found')"
[ "$FOUND" = "true" ] || blocked "unknown feature id '$FEATURE_ID' -- see docs/FEATURE_MAP.md"

PROBE="$(echo "$INFO_JSON" | jq -r '.probe')"
MODE="$(echo "$INFO_JSON" | jq -r '.mode')"
mapfile -t SPECS < <(echo "$INFO_JSON" | jq -r '.e2e[]')

if [ "${#SPECS[@]}" -eq 0 ]; then
  blocked "no covering e2e spec for '$FEATURE_ID' (docs/FEATURE_MAP.md's coverage table) -- this feature is checked by its own probe instead: $PROBE (run 'python -m rag_app.devtools.registry_check' for a real probe run)"
fi

SPEC_ARGS=()
for spec in "${SPECS[@]}"; do
  SPEC_ARGS+=("e2e/$spec")
done
echo "covering spec(s) for $FEATURE_ID: ${SPECS[*]}" | tee "$EVIDENCE_DIR/run.log"

RUN_LOG="$EVIDENCE_DIR/run.log"
EXIT_CODE=0

if [ -n "$BASE_URL" ]; then
  # DA-HB-2: refuse a MUTATING feature against a remote --base-url unless the operator
  # explicitly opted in -- checked BEFORE the reachability probe below so a mutating feature
  # is refused on its own merits, never by accident just because the target happened to be
  # unreachable. This does not change the no-"--base-url" (local bring-up) path at all.
  if [ "$MODE" = "mutating" ] && [ "$ALLOW_REMOTE_WRITES" -ne 1 ]; then
    blocked "'$FEATURE_ID' is covered by a MUTATING spec (docs/FEATURE_MAP.md marks it" \
      "'mutating' -- it creates/changes real remote state: account creation, e-mail sending," \
      "erasure, a lab run, or similar) and --base-url targets an ALREADY RUNNING app; refusing" \
      "to run it against $BASE_URL without --allow-remote-writes (only pass that flag if you" \
      "accept this run will mutate $BASE_URL's real state)"
  fi
  if [ "$MODE" = "mutating" ]; then
    echo "WARNING: --allow-remote-writes is set -- running a MUTATING spec against a REMOTE" \
      "target ($BASE_URL); this WILL create/change real remote state (account creation," \
      "e-mail sending, erasure, or a lab run)." | tee -a "$EVIDENCE_DIR/run.log" >&2
  fi
  if ! curl -fsS --max-time 5 -o /dev/null "$BASE_URL/"; then
    blocked "missing prerequisite: $BASE_URL is not reachable (start the app, or omit --base-url to bring up the local stack via 'scripts/gate.sh --only e2e')"
  fi
  echo "target: $BASE_URL (already running, not brought up by this script)" | tee -a "$RUN_LOG"
  GATE_LOCK_LABEL="verify:$FEATURE_ID" scripts/dev/with-lock.sh bash -c "
    cd '$REPO_ROOT/frontend' &&
    E2E_BASE_URL='$BASE_URL' npx playwright test ${SPEC_ARGS[*]} \
      --trace on --output '$EVIDENCE_DIR/test-results' --reporter=list
  " 2>&1 | tee -a "$RUN_LOG"
  EXIT_CODE="${PIPESTATUS[0]:-1}"
  if [ "$EXIT_CODE" -eq 0 ]; then
    echo "PASS: $FEATURE_ID (${SPECS[*]} against $BASE_URL)" | tee -a "$RUN_LOG"
  else
    echo "FAIL: $FEATURE_ID (${SPECS[*]} against $BASE_URL) -- see $RUN_LOG and" \
      "$EVIDENCE_DIR/test-results (Playwright traces: npx playwright show-trace <file>.zip)" \
      | tee -a "$RUN_LOG"
  fi
  echo "evidence: $EVIDENCE_DIR"
  exit "$EXIT_CODE"
fi

# No --base-url: delegate the local bring-up to scripts/gate.sh --only e2e (see the header
# comment above for why this is NOT also wrapped in with-lock.sh).
echo "target: local (bringing up the gate's own throwaway stack via 'scripts/gate.sh --only e2e')" \
  | tee -a "$RUN_LOG"
GATE_LOG_BEFORE="$(find "$HOME/secrag-gate-logs" -maxdepth 1 -name 'gate-*.log' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n1 | cut -d' ' -f2-)"
"$REPO_ROOT/scripts/gate.sh" --only e2e 2>&1 | tee -a "$EVIDENCE_DIR/gate-e2e.log"
GATE_EXIT="${PIPESTATUS[0]:-1}"
GATE_LOG_AFTER="$(find "$HOME/secrag-gate-logs" -maxdepth 1 -name 'gate-*.log' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n1 | cut -d' ' -f2-)"
if [ -n "$GATE_LOG_AFTER" ] && [ "$GATE_LOG_AFTER" != "$GATE_LOG_BEFORE" ]; then
  echo "gate.sh's own log: $GATE_LOG_AFTER" | tee -a "$RUN_LOG"
fi

if [ "$GATE_EXIT" -eq 0 ]; then
  # The whole fixed e2e bundle passed, so every spec in it -- including this feature's --
  # passed too.
  EXIT_CODE=0
  echo "PASS: $FEATURE_ID (${SPECS[*]}, part of the full 'e2e' gate bundle)" | tee -a "$RUN_LOG"
else
  # The bundle failed: only call it a FAIL for THIS feature if the failure actually mentions
  # one of its own spec file(s); otherwise an unrelated spec broke and this script cannot
  # honestly tell PASS from FAIL for this feature from that run alone.
  MATCHED=0
  for spec in "${SPECS[@]}"; do
    if grep -q "$spec" "$EVIDENCE_DIR/gate-e2e.log"; then
      MATCHED=1
    fi
  done
  if [ "$MATCHED" -eq 1 ] && grep -qiE "✘|✗| failed| FAIL" "$EVIDENCE_DIR/gate-e2e.log"; then
    EXIT_CODE=1
    echo "FAIL: $FEATURE_ID -- see $EVIDENCE_DIR/gate-e2e.log" | tee -a "$RUN_LOG"
  else
    EXIT_CODE=2
    echo "BLOCKED: gate.sh --only e2e failed, but not evidently in ${SPECS[*]} -- cannot" \
      "tell this feature's result from an unrelated failure; see $EVIDENCE_DIR/gate-e2e.log" \
      | tee -a "$RUN_LOG"
  fi
fi

echo "evidence: $EVIDENCE_DIR"
exit "$EXIT_CODE"
