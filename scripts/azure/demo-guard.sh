#!/usr/bin/env bash
# Demo guard (T11.6b.8, block H, 11b): the hourly safety net for demo mode
# (scripts/azure/demo-mode.sh, T11.6b.7). Scales any app whose `secrag-demo-until` tag has
# passed, OR that has min-replicas=1 with NO tag at all (e.g. someone changed it by hand),
# back to min-replicas 0. It can ONLY scale DOWN — there is no code path here that raises
# min-replicas or touches max-replicas, env vars or secrets. Called by
# .github/workflows/demo-guard.yml (hourly cron + workflow_dispatch), so it still runs with
# the user's PC off.
#
#   demo-guard.sh [--rg RG] [--backend-app A] [--ollama-app A] [--frontend-app A] [--dry-run]
#
# Flags default to the repo variables used by cd.yml and demo-mode.sh (AZURE_RESOURCE_GROUP,
# AZURE_BACKEND_APP, AZURE_OLLAMA_APP, AZURE_FRONTEND_APP).
#
# --dry-run prints what each app's state is and what WOULD be scaled down, without calling
# az at all (no Azure credentials needed) — used by the workflow's workflow_dispatch path.
set -euo pipefail

TAG_KEY="secrag-demo-until"

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { echo "::error::$*" >&2; exit 1; }
say() { echo "[demo-guard] $*"; }

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

to_epoch() { date -u -d "$1" +%s 2>/dev/null; }
is_past_or_invalid() {
  local epoch
  epoch="$(to_epoch "$1")" || return 0
  [ "$epoch" -le "$(date -u +%s)" ]
}

if [ "$dry_run" = 1 ]; then
  say "dry run — no az call will be made"
  for app in "${apps[@]}"; do
    say "would check $app: min-replicas + $TAG_KEY tag; scale to 0 only if min=1 AND (no tag OR tag expired)"
  done
  exit 0
fi

command -v az >/dev/null || die "az not found"

for app in "${apps[@]}"; do
  min="$(az containerapp show -g "$rg" -n "$app" --query properties.template.scale.minReplicas -o tsv)" \
    || die "cannot read min-replicas of $app"
  if [ "$min" != "1" ]; then
    say "$app: min-replicas=$min — already at rest, nothing to do"
    continue
  fi
  tag="$(az containerapp show -g "$rg" -n "$app" --query "tags.\"$TAG_KEY\"" -o tsv 2>/dev/null || true)"
  if [ -n "$tag" ] && ! is_past_or_invalid "$tag"; then
    say "$app: min-replicas=1, $TAG_KEY=$tag (still in the future) — leaving it running"
    continue
  fi
  say "$app: min-replicas=1 but ${tag:+expired at }$TAG_KEY=${tag:-<missing>} — scaling DOWN to 0"
  # --max-replicas is still passed (always 1) so this call can never silently change it;
  # it is NEVER raised above 1 anywhere in this script.
  az containerapp update -g "$rg" -n "$app" --min-replicas 0 --max-replicas 1 -o none \
    || die "az containerapp update failed for $app"
  id="$(az containerapp show -g "$rg" -n "$app" --query id -o tsv)" \
    || die "cannot read the resource id of $app"
  az tag update --resource-id "$id" --operation delete --tags "$TAG_KEY" -o none \
    || die "az tag update (delete) failed for $app"
  say "$app: scaled down, tag removed"
done
