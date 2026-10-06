#!/usr/bin/env bash
# Demo mode (T11.6b.7, block H, 11b): `on` wakes the 3 Azure apps (backend, Ollama,
# frontend) for a FIXED 3h window, so the cold start never shows up during an interview or
# a quick try-out; `off` goes back to the normal scale-0 posture; `status` reports the
# current state. The hourly `demo-guard.yml` workflow (T11.6b.8) is the real safety net: it
# scales down ANY app whose `secrag-demo-until` tag has expired (or is missing while min=1)
# even if the user's PC is off.
#
#   demo-mode.sh on     [--rg RG] [--backend-app A] [--ollama-app A] [--frontend-app A] [--dry-run]
#   demo-mode.sh off     ...same flags...
#   demo-mode.sh status  ...same flags...
#
# Flags default to the repo variables used by cd.yml (AZURE_RESOURCE_GROUP,
# AZURE_BACKEND_APP, AZURE_OLLAMA_APP, AZURE_FRONTEND_APP) read from the environment.
#
# on      1. For every app whose min-replicas is not already 1: `containerapp update
#            --min-replicas 1 --max-replicas 1` (max is ALWAYS pinned to 1 here — this
#            script never raises it, and an app already at min=1 is left alone: re-running
#            `on` while a demo is active only moves the end time, never restarts the apps).
#         2. Every app's `secrag-demo-until` tag is (re)written to now + 3h FIXED (no
#            option) via `az tag update --operation merge` (merge: no other tag is ever
#            touched, and no env var/secret is ever touched by this script at all).
#         3. Waits for each app's `runningStatus` to become `Running` (bounded), then the
#            backend's `/health` to answer 200 (`scripts/cd/health_check.sh`).
#         4. Prints "ready — ask a question in the browser" + the end time.
# off     Sets min-replicas back to 0 (max stays 1) and REMOVES the tag on every app, then
#         re-reads min-replicas to confirm the scale-down was accepted (the apps keep
#         running until Azure's own idle cooldown — this script does not wait for that).
# status  Per app: min-replicas, the `secrag-demo-until` tag (if any). Overall state is
#         `on` (every app min=1 and its tag is in the future), `expired` (min=1 but the tag
#         is in the past or missing — demo-guard.yml will scale it down within the hour) or
#         `off` (every app min=0). Prints the end time and an approximate cost so far
#         (`DEMO_MODE_HOURLY_COST_EUR`, default 0.17 EUR/h for the 3 apps together — see
#         PHASE_PLANNING §2.2 cost budget).
#
# --dry-run prints the exact `az` calls instead of running them (no `az` needed), like
# `scripts/cd/azure_jobs.sh`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HEALTH_CHECK="$REPO_ROOT/scripts/cd/health_check.sh"
TAG_KEY="secrag-demo-until"
DEMO_HOURS=3
POLL="${AZURE_DEMO_POLL_SECONDS:-10}"
RUNNING_TIMEOUT="${AZURE_DEMO_RUNNING_TIMEOUT_SECONDS:-240}"
HEALTH_TIMEOUT="${AZURE_DEMO_HEALTH_TIMEOUT_SECONDS:-240}"
HOURLY_COST_EUR="${DEMO_MODE_HOURLY_COST_EUR:-0.17}"

usage() { sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { echo "::error::$*" >&2; exit 1; }
say() { echo "[demo-mode] $*"; }

[ $# -gt 0 ] || usage
action="$1"
shift
case "$action" in on | off | status) ;; *) usage ;; esac

rg="${AZURE_RESOURCE_GROUP:-}"
backend_app="${AZURE_BACKEND_APP:-}"
ollama_app="${AZURE_OLLAMA_APP:-}"
frontend_app="${AZURE_FRONTEND_APP:-}"
dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    --rg) rg="${2:?}"; shift ;;
    --backend-app) backend_app="${2:?}"; shift ;;
    --ollama-app) ollama_app="${2:?}"; shift ;;
    --frontend-app) frontend_app="${2:?}"; shift ;;
    --dry-run) dry_run=1 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
  shift
done

[ -n "$rg" ] || die "--rg is required (repo variable AZURE_RESOURCE_GROUP)"
[ -n "$backend_app" ] || die "--backend-app is required (repo variable AZURE_BACKEND_APP)"
[ -n "$ollama_app" ] || die "--ollama-app is required (repo variable AZURE_OLLAMA_APP)"
[ -n "$frontend_app" ] || die "--frontend-app is required (repo variable AZURE_FRONTEND_APP)"

apps=("$backend_app" "$ollama_app" "$frontend_app")

# now_iso / plus_3h_iso / is_past / hours_between: tiny date helpers, GNU-date only (same
# toolchain as the rest of scripts/cd — no BSD date fallback needed, WSL/CI are both GNU).
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
plus_3h_iso() { date -u -d "+${DEMO_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ; }
to_epoch() { date -u -d "$1" +%s 2>/dev/null; }
is_future() { local epoch; epoch="$(to_epoch "$1")" || return 1; [ "$epoch" -gt "$(date -u +%s)" ]; }

if [ "$dry_run" = 1 ]; then
  end="$(plus_3h_iso)"
  case "$action" in
    on)
      say "would run (only for an app whose min-replicas is not already 1):"
      for app in "${apps[@]}"; do
        say "  would run: az containerapp update -g $rg -n $app --min-replicas 1 --max-replicas 1"
      done
      for app in "${apps[@]}"; do
        say "  would run: az tag update --resource-id <id of $app> --operation merge --tags $TAG_KEY=$end"
      done
      say "would wait for each app's runningStatus = Running (<= ${RUNNING_TIMEOUT}s)"
      say "would run: $HEALTH_CHECK https://<backend fqdn>/health --timeout $HEALTH_TIMEOUT"
      say "would print: ready — ask a question in the browser (until $end)"
      ;;
    off)
      for app in "${apps[@]}"; do
        say "would run: az containerapp update -g $rg -n $app --min-replicas 0 --max-replicas 1"
        say "would run: az tag update --resource-id <id of $app> --operation delete --tags $TAG_KEY"
      done
      say "would check: every app's min-replicas is now 0"
      ;;
    status)
      for app in "${apps[@]}"; do
        say "would run: az containerapp show -g $rg -n $app --query properties.template.scale.minReplicas -o tsv"
        say "would run: az containerapp show -g $rg -n $app --query tags.$TAG_KEY -o tsv"
      done
      ;;
  esac
  exit 0
fi

command -v az >/dev/null || die "az not found"

min_replicas_of() {
  az containerapp show -g "$rg" -n "$1" --query properties.template.scale.minReplicas -o tsv
}

tag_of() {
  az containerapp show -g "$rg" -n "$1" --query "tags.\"$TAG_KEY\"" -o tsv 2>/dev/null || true
}

resource_id_of() {
  az containerapp show -g "$rg" -n "$1" --query id -o tsv
}

set_min_replicas() {
  local app="$1" min="$2"
  az containerapp update -g "$rg" -n "$app" --min-replicas "$min" --max-replicas 1 -o none \
    || die "az containerapp update failed for $app"
  local now
  now="$(min_replicas_of "$app")" || die "cannot read min-replicas of $app"
  [ "$now" = "$min" ] || die "$app min-replicas is $now, expected $min"
}

merge_tag() {
  local app="$1" value="$2" id
  id="$(resource_id_of "$app")" || die "cannot read the resource id of $app"
  az tag update --resource-id "$id" --operation merge --tags "$TAG_KEY=$value" -o none \
    || die "az tag update (merge) failed for $app"
}

delete_tag() {
  local app="$1" id
  id="$(resource_id_of "$app")" || die "cannot read the resource id of $app"
  az tag update --resource-id "$id" --operation delete --tags "$TAG_KEY" -o none \
    || die "az tag update (delete) failed for $app"
}

wait_running() {
  local app="$1" deadline status
  deadline=$((SECONDS + RUNNING_TIMEOUT))
  while :; do
    status="$(az containerapp show -g "$rg" -n "$app" --query properties.runningStatus -o tsv 2>/dev/null)" \
      || status="unknown"
    [ "$status" = "Running" ] && { say "$app: Running"; return 0; }
    [ "$SECONDS" -lt "$deadline" ] || die "$app did not reach Running within ${RUNNING_TIMEOUT}s (last status: $status)"
    sleep "$POLL"
  done
}

case "$action" in
  on)
    end="$(plus_3h_iso)"
    for app in "${apps[@]}"; do
      current="$(min_replicas_of "$app")" || die "cannot read min-replicas of $app"
      if [ "$current" = "1" ]; then
        say "$app: already min-replicas 1 — extending only, no restart"
      else
        say "$app: waking (min-replicas 0 -> 1, max stays 1)"
        set_min_replicas "$app" 1
      fi
    done
    for app in "${apps[@]}"; do
      merge_tag "$app" "$end"
    done
    for app in "${apps[@]}"; do
      wait_running "$app"
    done
    backend_fqdn="$(az containerapp show -g "$rg" -n "$backend_app" \
      --query properties.configuration.ingress.fqdn -o tsv)" \
      || die "cannot read the backend's FQDN"
    bash "$HEALTH_CHECK" "https://$backend_fqdn/health" --timeout "$HEALTH_TIMEOUT"
    say "ready — ask a question in the browser (demo ends $end)"
    ;;
  off)
    for app in "${apps[@]}"; do
      say "$app: sleeping (min-replicas -> 0, max stays 1)"
      set_min_replicas "$app" 0
      delete_tag "$app"
    done
    say "demo mode off"
    ;;
  status)
    overall="off"
    end=""
    for app in "${apps[@]}"; do
      min="$(min_replicas_of "$app")" || die "cannot read min-replicas of $app"
      tag="$(tag_of "$app")"
      if [ "$min" = "1" ]; then
        if [ -n "$tag" ] && is_future "$tag"; then
          [ "$overall" != "off" ] || overall="on"
          end="$tag"
        else
          overall="expired"
          end="${tag:-none}"
        fi
      fi
      say "$app: min-replicas=$min secrag-demo-until=${tag:-<none>}"
    done
    say "state: $overall"
    if [ "$overall" != "off" ] && [ -n "$end" ] && [ "$end" != "none" ]; then
      end_epoch="$(to_epoch "$end")"
      start_epoch=$((end_epoch - DEMO_HOURS * 3600))
      now_epoch="$(date -u +%s)"
      elapsed_s=$((now_epoch - start_epoch))
      [ "$elapsed_s" -ge 0 ] || elapsed_s=0
      cost="$(awk -v s="$elapsed_s" -v rate="$HOURLY_COST_EUR" 'BEGIN { printf "%.2f", (s / 3600.0) * rate }')"
      say "ends: $end — approximate cost so far: EUR $cost"
    fi
    ;;
esac
