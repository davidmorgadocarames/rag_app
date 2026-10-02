#!/usr/bin/env bash
# Post-deploy health check (deferred from 11a, done in 11b block D): poll a URL until it
# answers the expected HTTP status, within a bounded timeout. Used by cd.yml's `deploy` job,
# AFTER the Container Apps are updated, so a broken revision fails the CD run instead of
# silently going live unnoticed (backend `/health`, frontend `/`).
#
#   health_check.sh <url> [--timeout <seconds>] [--interval <seconds>] [--expect-status <code>]
#
# No secrets: these are public ingress URLs (no token, no query string) — only the URL and
# the resulting HTTP status are ever printed, never a response body.
set -euo pipefail

url="${1:-}"
[ -n "$url" ] || { echo "health_check: a URL is required" >&2; exit 2; }
shift || true

timeout_s=180 interval_s=5 expect_status=200
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout) timeout_s="${2:?}"; shift ;;
    --interval) interval_s="${2:?}"; shift ;;
    --expect-status) expect_status="${2:?}"; shift ;;
    *) echo "health_check: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ "$timeout_s" =~ ^[0-9]+$ ]] || { echo "health_check: --timeout must be seconds" >&2; exit 2; }
[[ "$interval_s" =~ ^[0-9]+$ ]] || { echo "health_check: --interval must be seconds" >&2; exit 2; }
[[ "$expect_status" =~ ^[0-9]{3}$ ]] || { echo "health_check: --expect-status must be a 3-digit HTTP code" >&2; exit 2; }

command -v curl >/dev/null || { echo "health_check: curl not found" >&2; exit 1; }

deadline=$((SECONDS + timeout_s))
last_status=""
while :; do
  # No `-f`: a non-2xx response must still be seen (and compared below), not swallowed —
  # only a real connection-level failure (refused, timeout, DNS) falls back to "000".
  last_status="$(curl -4 -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)" || last_status="000"
  if [ "$last_status" = "$expect_status" ]; then
    echo "health_check: $url -> $last_status (ok)"
    exit 0
  fi
  [ "$SECONDS" -lt "$deadline" ] || break
  sleep "$interval_s"
done
echo "health_check: $url did not answer $expect_status within ${timeout_s}s (last status: ${last_status:-none})" >&2
exit 1
