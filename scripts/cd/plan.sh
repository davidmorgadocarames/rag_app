#!/usr/bin/env bash
# CD changed-files plan (T11.0.9, TF2; per-image outputs T13.CD.1): does <sha> need a build +
# deploy, and WHICH of the 4 images (backend, frontend, jobs, ollama)?
#
#   plan.sh --sha <sha> [--base <sha>] [--tip <sha>] [--force-all]
#
# Base = --base when given (dispatch input `base_sha`, dry runs only), otherwise the LAST
# DEPLOYED SHA: the newest GitHub deployment (environment "azure") whose latest status is
# `success`. Checks, in order (DA-C-1: CI runs finish in any order, so CD must never roll
# back and never deploy a superseded commit):
#   1. --tip given (the branch head NOW, read by CD at plan time) and <sha> is not it
#      → skip: a newer commit landed and its own CD run deploys it. A malformed --tip fails.
#   2. <sha> equals the base, or is an ANCESTOR of it → skip: not newer than what is deployed.
#   3. No base (nothing deployed yet), or a base unknown to this clone → deploy everything
#      (all 4 images build: no diff to tell them apart).
#   4. Only paths that never enter any of the 4 images changed → build and deploy are skipped
#      (`workflow_run` cannot use `paths-ignore`): `docs/**`, `**/*.md`, `deploy/k8s/**` (not
#      used yet), `deploy/prometheus/**` (dev compose only, never deployed), `eval/**`,
#      `data/**` (not read by any Dockerfile), `backend/tests/**`, `backend/requirements-
#      dev.txt`, `backend/pyproject.toml` (dev/lint-only, never `COPY`-ed), `scripts/azure/**`,
#      `scripts/dev/**`, `scripts/prereqs/**`, `scripts/gate.sh`, `scripts/fetch_corpus.py`,
#      `scripts/restart_check.*` (host-only ops/dev tooling) and `frontend/**/*.test.ts` (never
#      bundled into `.next/standalone`, DEBT-023, T11c.E5 option B — the inventory of what
#      each Dockerfile actually copies is in the PR). Deliberately NOT skipped, even though
#      most changes there are also harmless: `scripts/db/**` (2 of its 6 files ARE baked into
#      the jobs image — `backup.sh`/`backup_lib.sh` via the `dbscripts` build context),
#      `scripts/cd/**` (this script's own logic — self-referential risk), `db/roles.sql` and
#      `deploy/azure/jobs/*.yaml` (never read by CD, but a human must still re-apply them by
#      hand; silently skipping would remove that signal), the Dockerfiles/`.dockerignore`
#      themselves, and every GitHub Actions workflow. `test_cd_pipeline.py` cross-checks this
#      list against the Dockerfiles' actual `COPY` sources so a future Dockerfile change that
#      starts copying a listed path fails that test, not CD silently skipping a real change.
#   5. When build/deploy is NOT skipped, every changed, non-skippable path is mapped to the
#      image(s) it can affect (T13.CD.1): `backend/**` → backend AND jobs (both built from the
#      `./backend` context — see Dockerfile/Dockerfile.jobs); `frontend/**` → frontend;
#      `ollama/**` → ollama; `.github/workflows/cd.yml` or `scripts/cd/**` (this pipeline's own
#      logic) → all four. Any other non-skippable path (e.g. `scripts/db/**`, `db/roles.sql`,
#      `deploy/azure/jobs/*.yaml`, any other workflow) is UNMAPPED → all four (fail-safe: build
#      more, never less). `--force-all` (or the workflow's `force_all` dispatch input) sets all
#      four to true whenever build/deploy is not skipped, overriding the file-based mapping —
#      it does NOT turn a skip (docs-only, ancestor, not-the-tip, already-deployed) into a
#      deploy: that stays a pure safety decision, never widened by this flag.
#
# Needs a full clone (fetch-depth: 0) and, without --base, `gh` + GH_REPO. Writes sha/base/
# deploy/reason/build_backend/build_frontend/build_jobs/build_ollama to $GITHUB_OUTPUT when
# set. Exit 0 whatever the verdict; exit 1 when the deployments cannot be read (fail-closed,
# DA-C2-1) or --tip is malformed.
set -euo pipefail

sha="" base="" base_source="input" tip="" tip_given=0 force_all=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) sha="$2"; shift ;;
    --base) base="$2"; shift ;;
    --tip) tip="${2:-}"; tip_given=1; shift ;;
    --force-all) force_all=1 ;;
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

# Prints the newest successfully deployed SHA (nothing when none is recorded). Returns 1 on
# any API error (DA-C2-1): a failing `gh` inside a `for` word list or a `$(…)` does not trip
# `set -e`, and "no deployment" would silently mean "deploy everything" — fail closed.
last_deployed_sha() {
  local rows row id state
  rows="$(gh api "repos/$GH_REPO/deployments?environment=azure&per_page=30" \
            --jq '.[] | "\(.id):\(.sha)"')" || return 1
  for row in $rows; do
    [[ "$row" =~ ^[0-9]+:[0-9a-f]{40}$ ]] || return 1
    id="${row%%:*}"
    state="$(gh api "repos/$GH_REPO/deployments/$id/statuses?per_page=1" \
               --jq '.[0].state // ""')" || return 1
    if [ "$state" = success ]; then
      echo "${row#*:}"
      return 0
    fi
  done
}

if [ -z "$base" ]; then
  base_source="last deployed (GitHub Deployments, environment azure)"
  if ! base="$(last_deployed_sha)"; then
    echo "plan: cannot read the GitHub deployments (API error or unexpected output) — refusing to plan" >&2
    exit 1
  fi
fi

# skippable <path>: changes that never need a new image (DEBT-023, T11c.E5 option B — every
# pattern below was verified against the 4 Dockerfiles' actual COPY sources; see the header
# comment for the full reasoning and what was deliberately left out).
skippable() {
  case "$1" in
    docs/* | *.md | deploy/k8s/* | deploy/prometheus/* | eval/* | data/* \
      | backend/tests/* | backend/requirements-dev.txt | backend/pyproject.toml \
      | scripts/azure/* | scripts/dev/* | scripts/prereqs/* \
      | scripts/gate.sh | scripts/fetch_corpus.py | scripts/restart_check.* \
      | frontend/*.test.ts) return 0 ;;
    *) return 1 ;;
  esac
}

# T13.CD.1: maps every path in "$@" to the image(s) it can affect, ORing into the global
# build_backend/build_frontend/build_jobs/build_ollama (already-"true" flags stay true). A
# skippable path (see skippable() above) affects none. Anything else that is not under
# backend/**, frontend/** or ollama/** — including this pipeline's own files — is UNMAPPED and
# widens to all four (fail-safe: build more, never less; see header comment point 5).
images_for() {
  local path
  for path in "$@"; do
    skippable "$path" && continue
    case "$path" in
      .github/workflows/cd.yml | scripts/cd/*)
        build_backend=true; build_frontend=true; build_jobs=true; build_ollama=true ;;
      backend/*)
        build_backend=true; build_jobs=true ;;
      frontend/*)
        build_frontend=true ;;
      ollama/*)
        build_ollama=true ;;
      *)
        build_backend=true; build_frontend=true; build_jobs=true; build_ollama=true ;;
    esac
  done
}

known_base=""
if [ -n "$base" ] && git cat-file -e "$base^{commit}" 2>/dev/null; then
  known_base="$(git rev-parse "$base^{commit}")"
fi

deploy=true
build_backend=false build_frontend=false build_jobs=false build_ollama=false
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
  build_backend=true build_frontend=true build_jobs=true build_ollama=true
elif ! git cat-file -e "$base^{commit}" 2>/dev/null; then
  reason="base $base is not in this clone — deploy everything"
  build_backend=true build_frontend=true build_jobs=true build_ollama=true
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
    images_for "${code[@]}"
  fi
fi

# --force-all (T13.CD.1): only widens the per-image mapping once build/deploy is already
# going ahead — it never turns a safety skip (not-the-tip, ancestor, already-deployed,
# docs-only, nothing-changed) into a deploy. Idempotent with the "deploy everything, no
# diff available" branches above.
if [ "$force_all" = 1 ] && [ "$deploy" = true ]; then
  build_backend=true build_frontend=true build_jobs=true build_ollama=true
  reason="$reason (--force-all: building all four images)"
fi

echo "plan: sha=$sha base=${base:-none} ($base_source)${tip:+ tip=$tip}"
echo "plan: deploy=$deploy — $reason"
echo "plan: build_backend=$build_backend build_frontend=$build_frontend build_jobs=$build_jobs build_ollama=$build_ollama"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "sha=$sha"
    echo "base=$base"
    echo "deploy=$deploy"
    echo "reason=$reason"
    echo "build_backend=$build_backend"
    echo "build_frontend=$build_frontend"
    echo "build_jobs=$build_jobs"
    echo "build_ollama=$build_ollama"
  } >>"$GITHUB_OUTPUT"
fi
