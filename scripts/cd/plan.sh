#!/usr/bin/env bash
# CD changed-files plan (T11.0.9, TF2): does <sha> need a build + deploy?
#
#   plan.sh --sha <sha> [--base <sha>] [--tip <sha>]
#
# Base = --base when given (dispatch input `base_sha`, dry runs only), otherwise the LAST
# DEPLOYED SHA: the newest GitHub deployment (environment "azure") whose latest status is
# `success`. Checks, in order (DA-C-1: CI runs finish in any order, so CD must never roll
# back and never deploy a superseded commit):
#   1. --tip given (the branch head NOW, read by CD at plan time) and <sha> is not it
#      → skip: a newer commit landed and its own CD run deploys it. A malformed --tip fails.
#   2. <sha> equals the base, or is an ANCESTOR of it → skip: not newer than what is deployed.
#   3. No base (nothing deployed yet), or a base unknown to this clone → deploy everything.
#   4. Only `docs/**`, `**/*.md` and `deploy/k8s/**` changed → build and deploy are skipped
#      (`workflow_run` cannot use `paths-ignore`).
#
# Needs a full clone (fetch-depth: 0) and, without --base, `gh` + GH_REPO. Writes
# sha/base/deploy/reason to $GITHUB_OUTPUT when set. Exit 0 whatever the verdict.
set -euo pipefail

sha="" base="" base_source="input" tip="" tip_given=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) sha="$2"; shift ;;
    --base) base="$2"; shift ;;
    --tip) tip="${2:-}"; tip_given=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$sha" ] || { echo "plan: --sha is required" >&2; exit 2; }
sha="$(git rev-parse --verify "$sha^{commit}")"
if [ "$tip_given" = 1 ] && ! [[ "$tip" =~ ^[0-9a-f]{40}$ ]]; then
  echo "plan: --tip must be the branch head's full SHA, got '$tip' — refusing to plan" >&2
  exit 1
fi

last_deployed_sha() {
  local row id state
  for row in $(gh api "repos/$GH_REPO/deployments?environment=azure&per_page=30" \
                 --jq '.[] | "\(.id):\(.sha)"'); do
    id="${row%%:*}"
    state="$(gh api "repos/$GH_REPO/deployments/$id/statuses?per_page=1" --jq '.[0].state // ""')"
    if [ "$state" = success ]; then
      echo "${row#*:}"
      return 0
    fi
  done
}

if [ -z "$base" ]; then
  base_source="last deployed (GitHub Deployments, environment azure)"
  base="$(last_deployed_sha)"
fi

# skippable <path>: changes that never need a new image.
skippable() {
  case "$1" in
    docs/* | deploy/k8s/* | *.md) return 0 ;;
    *) return 1 ;;
  esac
}

known_base=""
if [ -n "$base" ] && git cat-file -e "$base^{commit}" 2>/dev/null; then
  known_base="$(git rev-parse "$base^{commit}")"
fi

deploy=true
if [ "$tip_given" = 1 ] && [ "$tip" != "$sha" ]; then
  deploy=false
  reason="${sha:0:12} is no longer the branch tip (now ${tip:0:12}) — skipped; the newer commit's own CD run deploys it"
elif [ -n "$known_base" ] && [ "$known_base" = "$sha" ]; then
  base="$known_base"
  deploy=false
  reason="${sha:0:12} is the deployed SHA — nothing changed, build/deploy skipped"
elif [ -n "$known_base" ] && git merge-base --is-ancestor "$sha" "$known_base"; then
  base="$known_base"
  deploy=false
  reason="${sha:0:12} is older than the deployed ${base:0:12} (an ancestor of it) — skipped; CD never rolls back"
elif [ -z "$base" ]; then
  reason="no successful deployment recorded yet — deploy everything"
elif ! git cat-file -e "$base^{commit}" 2>/dev/null; then
  reason="base $base is not in this clone — deploy everything"
else
  base="$(git rev-parse "$base^{commit}")"
  mapfile -t changed < <(git diff --name-only "$base" "$sha")
  code=()
  for path in "${changed[@]}"; do
    skippable "$path" || code+=("$path")
  done
  echo "changed files ${base:0:12}..${sha:0:12}: ${#changed[@]} (${#code[@]} need a deploy)"
  printf '  %s\n' "${changed[@]:0:50}"
  [ "${#changed[@]}" -le 50 ] || echo "  … and $(( ${#changed[@]} - 50 )) more"
  if [ "${#changed[@]}" -eq 0 ]; then
    deploy=false
    reason="nothing changed since ${base:0:12} — build/deploy skipped"
  elif [ "${#code[@]}" -eq 0 ]; then
    deploy=false
    reason="only docs/**, **/*.md or deploy/k8s/** changed since ${base:0:12} — build/deploy skipped"
  else
    reason="${#code[@]} file(s) outside docs/**, **/*.md, deploy/k8s/** changed since ${base:0:12} (e.g. ${code[0]}) — build and deploy"
  fi
fi

echo "plan: sha=$sha base=${base:-none} ($base_source)${tip:+ tip=$tip}"
echo "plan: deploy=$deploy — $reason"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "sha=$sha"
    echo "base=$base"
    echo "deploy=$deploy"
    echo "reason=$reason"
  } >>"$GITHUB_OUTPUT"
fi
