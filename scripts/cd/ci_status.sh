#!/usr/bin/env bash
# Did CI pass on exactly this commit, pushed to this branch? (DA-C-2, D-2026-09-29-1 a)
#
#   ci_status.sh verify <sha> [--branch <name>] [--workflow <file>]
#
# rc 0 when the CI workflow (default ci.yml) has at least one run with event `push`,
# head_branch <branch> (default main), head_sha <sha> and conclusion `success`; otherwise
# prints "REFUSED" and exits 1. An API error also refuses (fail-closed). CD runs it before a
# real manual deploy (workflow_dispatch, dry_run=false on main): the workflow_run path
# already starts only after CI succeeded, the manual path did not check it.
#
# GH_REPO=<owner>/<repo> selects the repository (default: the current clone's, via gh).
# Needs `gh` with a token that may read Actions runs (`actions: read` in CD).
set -euo pipefail

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

cmd="${1:-}"
sha="${2:-}"
if [ "$cmd" != verify ] || [ -z "$sha" ]; then
  usage >&2
  exit 2
fi
shift 2
branch=main workflow=ci.yml
while [ $# -gt 0 ]; do
  case "$1" in
    --branch) branch="$2"; shift ;;
    --workflow) workflow="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ci_status: <sha> must be a full 40-character commit SHA, got '$sha'" >&2
  exit 2
fi
command -v gh >/dev/null || { echo "ci_status: gh not found" >&2; exit 1; }
if [ -z "${GH_REPO:-}" ]; then
  GH_REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)" || exit 1
fi

# The query parameters narrow the list; the jq filter re-checks every field, so a
# parameter the API ignores can never widen what counts.
filter="[.workflow_runs[] | select(.head_sha == \"$sha\" and .head_branch == \"$branch\"
          and .event == \"push\" and .conclusion == \"success\")] | length"
if ! count="$(gh api "repos/$GH_REPO/actions/workflows/$workflow/runs?head_sha=$sha&branch=$branch&event=push&status=success&per_page=100" \
                --jq "$filter")"; then
  echo "ci_status: REFUSED — could not read the $workflow runs of $sha (API error)"
  exit 1
fi
if [[ "$count" =~ ^[0-9]+$ ]] && [ "$count" -gt 0 ]; then
  echo "ci_status: $workflow succeeded on a push of $sha to $branch ($count run(s)) — CI requirement met"
  exit 0
fi
echo "ci_status: REFUSED — no successful $workflow push run for $sha on $branch. Push it to $branch and let CI pass first."
exit 1
