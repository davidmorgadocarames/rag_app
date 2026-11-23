#!/usr/bin/env bash
# scripts/dev/with-lock.sh <cmd> [args...]
#
# Takes the SAME global gate lock scripts/gate.sh uses (T13.14.4, Paralelo-B problem #4:
# "agents' own Docker/e2e/servers collide on ports") before running <cmd>: an agent's own
# dev stack, e2e run or server shares the native Ollama and, if it reuses gate.sh's fixed
# ports/compose project by mistake, the same resources a concurrent `scripts/gate.sh` run
# needs -- this serializes the two instead of letting them collide. Plain unit tests (no
# Docker, no Ollama) never need this wrapper -- they stay lock-free.
#
# Usage: scripts/dev/with-lock.sh <cmd> [args...]
#
# Env (same defaults/semantics as scripts/gate.sh):
#   GATE_LOCK_FILE     lock file path (must match gate.sh's own for the two to serialize
#                       against each other; default: ${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag/gate.lock)
#   GATE_LOCK_TIMEOUT   seconds to wait before failing (default 3600 = 60 min)
#   GATE_LOCK_LABEL     name recorded in the holder info (default: this shell's cwd)
set -uo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") <cmd> [args...]" >&2
  exit 2
fi

# Only GATE_LOCK_FILE's own directory is ever created (never a separately-defaulted
# GATE_LOCK_DIR), so an explicit override never touches $HOME -- same as gate.sh's own lock.
GATE_LOCK_FILE="${GATE_LOCK_FILE:-${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag/gate.lock}"
GATE_LOCK_HOLDER_FILE="$GATE_LOCK_FILE.holder"
GATE_LOCK_TIMEOUT="${GATE_LOCK_TIMEOUT:-3600}"

mkdir -p "$(dirname "$GATE_LOCK_FILE")" 2>/dev/null
exec {GATE_LOCK_FD}>"$GATE_LOCK_FILE" \
  || { echo "FAIL: cannot open gate lock file $GATE_LOCK_FILE" >&2; exit 1; }

if ! flock -n "$GATE_LOCK_FD"; then
  holder="$(cat "$GATE_LOCK_HOLDER_FILE" 2>/dev/null)"
  echo "with-lock: waiting for the gate lock held by ${holder:-another run} (timeout ${GATE_LOCK_TIMEOUT}s; set GATE_LOCK_TIMEOUT to change)" >&2
  if ! flock -w "$GATE_LOCK_TIMEOUT" "$GATE_LOCK_FD"; then
    echo "FAIL: timed out after ${GATE_LOCK_TIMEOUT}s waiting for the gate lock held by ${holder:-another run}" >&2
    exit 1
  fi
fi

printf 'block=%s branch=%s pid=%s mode=%s start=%s\n' \
  "${GATE_LOCK_LABEL:-$(basename "$PWD")}" \
  "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)" \
  "$$" "with-lock:$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >"$GATE_LOCK_HOLDER_FILE" 2>/dev/null

"$@"
