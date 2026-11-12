#!/usr/bin/env bash
# Exposed-files check (T12.5.7, user idea 2026-11-09, DEC-023): the frontend's public URL
# must never serve any of these paths with real content — a non-200 response is "safe"
# (404 is expected; any other non-2xx also passes, the point is "no content ever comes
# back"), a 200 is the failure. The backend has no static file serving at all (proven
# separately and unconditionally, backend/tests/security/test_exposed_files.py) — this
# script only ever probes the FRONTEND's public URL, the one surface a misconfigured
# static server could theoretically leak a repo file through.
#
#   exposed_files_check.sh <frontend_base_url>
#
# No secrets: a public ingress URL with a fixed, hardcoded path list — only the URL and the
# resulting HTTP status are ever printed, never a response body (same discipline as
# scripts/cd/health_check.sh). Used by the Azure smoke (T12.6b.6) and callable locally
# against any running frontend (`npm run dev`/`start`, or the gate's own e2e stack).
set -euo pipefail

base="${1:-}"
[ -n "$base" ] || { echo "exposed_files_check: a frontend base URL is required" >&2; exit 2; }
base="${base%/}"

# One representative path per T12.5.7 category — never a wildcard (curl cannot probe one),
# so each stands in for its whole class (``.next/`` server internals, a ``*.map`` source
# map, a dotfile, the backend's own compose service name).
PATHS=(
  "/.git/HEAD"
  "/.git/config"
  "/.env"
  "/.env.local"
  "/.next/BUILD_ID"
  "/_next/static/chunks/main.js.map"
  "/backend/"
  "/docker-compose.yml"
)

command -v curl >/dev/null || { echo "exposed_files_check: curl not found" >&2; exit 1; }

exit_code=0
for path in "${PATHS[@]}"; do
  # No `-f`: a non-2xx response must still be seen (and compared below), not swallowed —
  # only a real connection-level failure (refused, timeout, DNS) falls back to "000", which
  # is also a PASS here (no content came back, whatever the reason).
  status="$(curl -4 -sS -o /dev/null -w '%{http_code}' --max-time 10 "${base}${path}" 2>/dev/null)" \
    || status="000"
  if [ "$status" = "200" ]; then
    echo "exposed_files_check: ${base}${path} -> 200 (EXPOSED)" >&2
    exit_code=1
  else
    echo "exposed_files_check: ${base}${path} -> $status (ok)"
  fi
done

exit "$exit_code"
