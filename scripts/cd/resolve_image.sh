#!/usr/bin/env bash
# Resolves one image reference for the per-image CD deploy (T13.CD.2): either the digest just
# built in this run, or — when that image did not change — the digest CURRENTLY DEPLOYED on
# Azure, read from the running Container App (`app`) or Container Apps Job (`job`). Fail-closed
# (X1 spirit): an unreadable `az` call, or a kept image that is not pinned by digest, stops the
# pipeline — never silently falls back to a floating tag.
#
#   resolve_image.sh app --rg RG --name NAME --repo REPO [--build true|false] [--built-digest sha256:…]
#   resolve_image.sh job --rg RG --name NAME --repo REPO [--build true|false] [--built-digest sha256:…]
#
# --build true   prints "$repo@$built-digest" (the image just pushed by this run).
# --build false  reads `properties.template.containers[0].image` from the app/Job named --name
#                and prints it verbatim IF it is pinned by digest (`@sha256:`); otherwise fails.
# Prints the resolved "repo@sha256:…" on stdout. Exit 0 on success, 1 on any Azure/pinning
# failure, 2 on a usage error.
set -euo pipefail

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { echo "resolve_image: $*" >&2; exit 1; }

[ $# -gt 0 ] || usage
kind="$1"
shift
case "$kind" in app | job) ;; *) usage ;; esac

rg="" name="" repo="" build="false" built_digest=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rg) rg="${2:?}"; shift ;;
    --name) name="${2:?}"; shift ;;
    --repo) repo="${2:?}"; shift ;;
    --build) build="${2:?}"; shift ;;
    --built-digest) built_digest="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
  shift
done
[ -n "$rg" ] || { echo "--rg is required" >&2; usage; }
[ -n "$name" ] || { echo "--name is required" >&2; usage; }
[ -n "$repo" ] || { echo "--repo is required" >&2; usage; }
case "$build" in true | false) ;; *) echo "--build must be true or false, got '$build'" >&2; usage ;; esac

if [ "$build" = true ]; then
  [[ "$built_digest" == sha256:* ]] || die "--build true needs --built-digest=sha256:… (got '${built_digest:-empty}')"
  echo "${repo}@${built_digest}"
  exit 0
fi

command -v az >/dev/null || die "az not found"

if [ "$kind" = app ]; then
  current="$(az containerapp show --name "$name" --resource-group "$rg" \
               --query 'properties.template.containers[0].image' -o tsv 2>/dev/null)" \
    || die "could not read the currently deployed image of app '$name' (resource group '$rg') — refusing (fail-closed)"
else
  current="$(az containerapp job show --name "$name" --resource-group "$rg" \
               --query 'properties.template.containers[0].image' -o tsv 2>/dev/null)" \
    || die "could not read the currently deployed image of Job '$name' (resource group '$rg') — refusing (fail-closed)"
fi
case "$current" in
  *@sha256:*) echo "$current" ;;
  *) die "kept image '${current:-empty}' for '$name' is not pinned by digest — refusing (fail-closed)" ;;
esac
