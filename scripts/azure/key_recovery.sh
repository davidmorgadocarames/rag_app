#!/usr/bin/env bash
# Entry point of the master-key recovery tool (D-2026-09-29-2; scripts/azure/key_recovery.py).
#
#   scripts/azure/key_recovery.sh init | check … | rewrap … | erase … | shred   (--help)
#
# Runs the tool with the backend venv's Python in isolated mode (-I: no PYTHON* variables, no
# user site, no current directory on sys.path; -B: no bytecode written) and with core dumps
# disabled (here, and in the tool itself: RLIMIT_CORE 0 + prctl PR_SET_DUMPABLE 0), so
# candidate keys can reach neither a dump nor a cache file. On Azure it runs INSIDE
# scripts/azure/db-tunnel.sh (which allows exactly this script besides psql/pg_dump); `erase`
# is local-only and refuses to run there.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
# The backend venv of this checkout (it has cryptography + psycopg); KEY_RECOVERY_PYTHON
# overrides it where there is none (the gate's pre-push worktree, CI).
PY="${KEY_RECOVERY_PYTHON:-$ROOT/backend/.venv/bin/python}"
[ -x "$PY" ] || { echo "key_recovery: no Python at $PY (backend venv missing?)" >&2; exit 2; }
ulimit -c 0
exec "$PY" -I -B "$ROOT/scripts/azure/key_recovery.py" "$@"
