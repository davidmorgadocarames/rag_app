#!/usr/bin/env bash
# Demo mode (T11.6b.7, block H, 11b; status/rollback hardened in the block H2 DA-review
# fix, 2026-10-06): `on` wakes the 3 Azure apps (backend, Ollama, frontend) for a FIXED 3h
# window, so the cold start never shows up during an interview or a quick try-out; `off`
# goes back to the normal scale-0 posture; `status` reports the current state. The hourly
# `demo-guard.yml` workflow (T11.6b.8) is the real safety net: it scales down ANY app whose
# `secrag-demo-until` tag has expired (or is missing while min=1) even if the user's PC is
# off.
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
#         If step 3 fails (an app never reaches Running, or /health never answers), `on`
#         rolls back everything it changed — every app it scaled goes back to min-replicas
#         0, every tag it wrote is removed — then exits non-zero. It never leaves apps up
#         with a valid future tag but no actually-ready demo (DA-11bH-2).
# off     Sets min-replicas back to 0 (max stays 1) and REMOVES the tag on every app, then
#         re-reads min-replicas to confirm the scale-down was accepted (the apps keep
#         running until Azure's own idle cooldown — this script does not wait for that).
# status  Per app: min-replicas, the `secrag-demo-until` tag (if any). Overall state checks
#         ALL THREE apps (DA-11bH-1 — a one-way latch that never downgraded `on` used to
#         hide an asleep app): `on` only if every app is min-replicas 1 with a future tag;
#         `expired` if any app is min-replicas 1 with a missing/expired/malformed tag
#         (demo-guard.yml will scale it down within the hour); `off` only if every app is
#         min-replicas 0; `partial` for any other mix (e.g. one app asleep while the other
#         two are awake) — deliberately never reported as `on`. Prints the end time and an
#         approximate cost so far (`DEMO_MODE_HOURLY_COST_EUR`, default 0.17 EUR/h for the 3
#         apps together — see PHASE_PLANNING §2.2 cost budget) for the `on`/`expired` cases.
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

usage() { sed -n '2,39p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
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
      say "on failure: would roll back (scale every touched app to 0, remove every tag written)"
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

# set_min_replicas / merge_tag / delete_tag / wait_running report failure by returning 1
# (never by exiting the whole script): `on` needs to catch a failure after it has already
# scaled/tagged apps and roll back, so none of these die() directly (DA-11bH-2). Callers
# that have nothing to roll back (`off`, `status`) chain `|| die ...` themselves.
set_min_replicas() {
  local app="$1" min="$2" now
  if ! az containerapp update -g "$rg" -n "$app" --min-replicas "$min" --max-replicas 1 -o none; then
    echo "::error::az containerapp update failed for $app" >&2
    return 1
  fi
  now="$(min_replicas_of "$app")" || { echo "::error::cannot read min-replicas of $app" >&2; return 1; }
  if [ "$now" != "$min" ]; then
    echo "::error::$app min-replicas is $now, expected $min" >&2
    return 1
  fi
}

merge_tag() {
  local app="$1" value="$2" id
  id="$(resource_id_of "$app")" || { echo "::error::cannot read the resource id of $app" >&2; return 1; }
  if ! az tag update --resource-id "$id" --operation merge --tags "$TAG_KEY=$value" -o none; then
    echo "::error::az tag update (merge) failed for $app" >&2
    return 1
  fi
}

delete_tag() {
  local app="$1" id
  id="$(resource_id_of "$app")" || { echo "::error::cannot read the resource id of $app" >&2; return 1; }
  if ! az tag update --resource-id "$id" --operation delete --tags "$TAG_KEY" -o none; then
    echo "::error::az tag update (delete) failed for $app" >&2
    return 1
  fi
}

wait_running() {
  local app="$1" deadline status
  deadline=$((SECONDS + RUNNING_TIMEOUT))
  while :; do
    status="$(az containerapp show -g "$rg" -n "$app" --query properties.runningStatus -o tsv 2>/dev/null)" \
      || status="unknown"
    [ "$status" = "Running" ] && { say "$app: Running"; return 0; }
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "::error::$app did not reach Running within ${RUNNING_TIMEOUT}s (last status: $status)" >&2
      return 1
    fi
    sleep "$POLL"
  done
}

# rollback_on() is best-effort: it runs after `do_on` has already failed, so it ignores
# further az failures (logged, not fatal) rather than compounding the original error.
rollback_on() {
  local app id
  say "rolling back 'on': scaling every app back to 0 and removing $TAG_KEY"
  for app in "${apps[@]}"; do
    az containerapp update -g "$rg" -n "$app" --min-replicas 0 --max-replicas 1 -o none 2>/dev/null \
      || say "rollback: could not scale $app back to 0 — check it manually"
    id="$(resource_id_of "$app" 2>/dev/null)" || continue
    az tag update --resource-id "$id" --operation delete --tags "$TAG_KEY" -o none 2>/dev/null \
      || say "rollback: could not remove the tag on $app — check it manually"
  done
}

# do_on() returns 1 (never exits) on any failure so the `on` case below can roll back
# (DA-11bH-2) before dying. Because it is called as `do_on || …`, bash suspends `set -e`
# for its body: a failing command inside it returns normally instead of aborting the script.
do_on() {
  local app current end
  end="$(plus_3h_iso)"
  for app in "${apps[@]}"; do
    current="$(min_replicas_of "$app")" || { echo "::error::cannot read min-replicas of $app" >&2; return 1; }
    if [ "$current" = "1" ]; then
      say "$app: already min-replicas 1 — extending only, no restart"
    else
      say "$app: waking (min-replicas 0 -> 1, max stays 1)"
      set_min_replicas "$app" 1 || return 1
    fi
  done
  for app in "${apps[@]}"; do
    merge_tag "$app" "$end" || return 1
  done
  for app in "${apps[@]}"; do
    wait_running "$app" || return 1
  done
  backend_fqdn="$(az containerapp show -g "$rg" -n "$backend_app" \
    --query properties.configuration.ingress.fqdn -o tsv)" \
    || { echo "::error::cannot read the backend's FQDN" >&2; return 1; }
  bash "$HEALTH_CHECK" "https://$backend_fqdn/health" --timeout "$HEALTH_TIMEOUT" || return 1
  say "ready — ask a question in the browser (demo ends $end)"
}

case "$action" in
  on)
    do_on || { rollback_on; die "'on' failed and was rolled back — every app is back at min-replicas 0, no tag left"; }
    ;;
  off)
    for app in "${apps[@]}"; do
      say "$app: sleeping (min-replicas -> 0, max stays 1)"
      set_min_replicas "$app" 0 || die "failed to scale $app to 0"
      delete_tag "$app" || die "failed to remove the tag on $app"
    done
    say "demo mode off"
    ;;
  status)
    # Per-app aggregation (DA-11bH-1): count how many of the 3 apps are cleanly "on"
    # (min=1 + future tag), cleanly "off" (min=0) or "bad" (min=1 but missing/expired/
    # malformed tag). `on` requires ALL apps on; `off` requires ALL apps off; ANY bad app
    # forces `expired`; anything else (a mix of on and off, no bad app) is `partial`.
    on_count=0
    off_count=0
    bad_count=0
    end=""
    for app in "${apps[@]}"; do
      min="$(min_replicas_of "$app")" || die "cannot read min-replicas of $app"
      tag="$(tag_of "$app")"
      if [ "$min" = "1" ]; then
        if [ -n "$tag" ] && is_future "$tag"; then
          on_count=$((on_count + 1))
          end="$tag"
        else
          bad_count=$((bad_count + 1))
          end="${tag:-none}"
        fi
      else
        off_count=$((off_count + 1))
      fi
      say "$app: min-replicas=$min secrag-demo-until=${tag:-<none>}"
    done
    if [ "$bad_count" -gt 0 ]; then
      overall="expired"
    elif [ "$on_count" -eq "${#apps[@]}" ]; then
      overall="on"
    elif [ "$off_count" -eq "${#apps[@]}" ]; then
      overall="off"
    else
      overall="partial"
    fi
    say "state: $overall"
    if [ "$overall" = "partial" ]; then
      say "apps do not agree on state — demo is NOT reliably ready (an asleep app could still cold-start-fail the first question)"
    fi
    if { [ "$overall" = "on" ] || [ "$overall" = "expired" ]; } && [ -n "$end" ] && [ "$end" != "none" ]; then
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
