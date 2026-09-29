#!/usr/bin/env bash
# Entry point of the master-key recovery tool (D-2026-09-29-2; scripts/azure/key_recovery.py).
#
#   scripts/azure/key_recovery.sh init | check … | rewrap … | shred     (--help for details)
#
# Runs the tool with the backend venv's Python in isolated mode (-I: no PYTHON* variables, no
# user site, no current directory on sys.path; -B: no bytecode written) and with core dumps
# disabled, so candidate keys can reach neither a dump nor a cache file. On Azure it runs
# INSIDE scripts/azure/db-tunnel.sh (which allows exactly this script besides psql/pg_dump).
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || { echo "key_recovery: backend venv missing at $PY" >&2; exit 2; }
ulimit -c 0
exec "$PY" -I -B "$ROOT/scripts/azure/key_recovery.py" "$@"
