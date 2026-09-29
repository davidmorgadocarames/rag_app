#!/usr/bin/env bash
# Azure Container Apps Jobs steps of CD (T11.2.6 + T11.2.11, X1). Called by cd.yml.
#
#   azure_jobs.sh migrate      --rg RG --job NAME --image IMG@sha256:… [--timeout S]
#   azure_jobs.sh update-image --rg RG --job NAME --image IMG@sha256:…
#   … [--dry-run] [--simulate-failure]
#
# migrate       1. `job update --image` (the jobs image, BY DIGEST) and check the Job now
#                  carries exactly that image;
#               2. `job start` with NO overrides (never a per-execution image/command: it can
#                  drop the Job's env/secret settings, X1);
#               3. wait for that execution: Succeeded → rc 0; Failed/Stopped/Degraded or the
#                  timeout (default 900 s) → rc 1, so CD stops BEFORE the apps are updated.
# update-image  `job update --image` + the same check (purge/backup Jobs follow the deployed
#               digest after the apps, X1).
#
# Never enables schedules, never creates Jobs, never applies YAML (manual, per runbook).
# --dry-run prints the exact commands instead of running them (no az needed);
# --simulate-failure (dry runs only) makes the migration execution "fail", to show that the
# pipeline stops there (T11.2.6 Done-when).
set -euo pipefail

POLL="${AZURE_JOBS_POLL_SECONDS:-10}"

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { echo "::error::$*" >&2; exit 1; }

[ $# -gt 0 ] || usage
action="$1"
shift
case "$action" in migrate | update-image) ;; *) usage ;; esac

rg="" job="" image="" timeout_s=900 dry_run=0 simulate=0
while [ $# -gt 0 ]; do
  case "$1" in
    --rg) rg="${2:?}"; shift ;;
    --job) job="${2:?}"; shift ;;
    --image) image="${2:?}"; shift ;;
    --timeout) timeout_s="${2:?}"; shift ;;
    --dry-run) dry_run=1 ;;
    --simulate-failure) simulate=1 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
  shift
done

[ -n "$rg" ] || die "--rg is required (repo variable AZURE_RESOURCE_GROUP)"
[ -n "$job" ] || die "--job is required (repo variables AZURE_MIGRATE_JOB / AZURE_PURGE_JOB / AZURE_BACKUP_JOB)"
[[ "$image" == *@sha256:* ]] || die "the image must be pinned by digest (got '${image:-empty}')"
[[ "$timeout_s" =~ ^[0-9]+$ ]] || die "--timeout must be seconds"
[ "$simulate" = 0 ] || [ "$dry_run" = 1 ] || die "--simulate-failure is for dry runs only"

say() { echo "[azure-jobs] $*"; }

if [ "$dry_run" = 1 ]; then
  say "would run: az containerapp job update -n $job -g $rg --image $image"
  say "would check: the Job's image is exactly $image"
  if [ "$action" = migrate ]; then
    say "would run: az containerapp job start -n $job -g $rg   (no overrides)"
    say "would wait (≤ ${timeout_s}s) for that execution: Succeeded → continue; otherwise STOP"
    if [ "$simulate" = 1 ]; then
      say "simulated: migration execution status Failed"
      die "migration Job $job failed (simulated) — the apps are NOT updated"
    fi
  fi
  exit 0
fi

command -v az >/dev/null || die "az not found"

update_image() {
  local now
  say "update: $job → $image"
  az containerapp job update -n "$job" -g "$rg" --image "$image" -o none \
    || die "az containerapp job update failed for $job"
  now="$(az containerapp job show -n "$job" -g "$rg" \
           --query 'properties.template.containers[0].image' -o tsv)" \
    || die "cannot read the image of $job"
  [ "$now" = "$image" ] || die "$job runs '$now', expected '$image'"
  say "$job image = $image"
}

update_image

if [ "$action" = migrate ]; then
  execution="$(az containerapp job start -n "$job" -g "$rg" --query name -o tsv)" \
    || die "az containerapp job start failed for $job"
  [ -n "$execution" ] || die "job start returned no execution name for $job"
  say "started execution $execution (no overrides); waiting up to ${timeout_s}s"
  deadline=$((SECONDS + timeout_s))
  while :; do
    status="$(az containerapp job execution show -n "$job" -g "$rg" \
                --job-execution-name "$execution" --query properties.status -o tsv 2>/dev/null)" \
      || status="unknown"
    case "$status" in
      Succeeded) say "execution $execution: Succeeded"; exit 0 ;;
      Failed | Stopped | Degraded)
        die "migration Job $job execution $execution: $status — the apps are NOT updated (logs: az containerapp job logs show -n $job -g $rg --execution $execution)" ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] \
      || die "migration Job $job execution $execution did not finish in ${timeout_s}s (last status: $status) — the apps are NOT updated"
    sleep "$POLL"
  done
fi
