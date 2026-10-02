#!/usr/bin/env bash
# The `secrag/gate-full` commit status (D-2026-09-27-7 b): proof that `scripts/gate.sh --full`
# passed on exactly this SHA. The gate publishes it; CD refuses to deploy a SHA without it.
#
#   gate_status.sh publish <sha> [--wait <s>] [--description <text>]
#       Post state=success for <sha>. The commit must already be on GitHub: with --wait it
#       polls until it is (the pre-push hook gates BEFORE the push lands). rc 3 = the commit
#       did not appear within the wait; rc 1 = any other error.
#   gate_status.sh verify <sha> [--wait <s>] [--creator <login>]
#       rc 0 when the NEWEST secrag/gate-full status of <sha> is `success` (and, with
#       --creator, was posted by that account); otherwise prints "REFUSED" and exits 1.
#       --wait polls for a status that is not there yet (a background publisher may lag).
#
# GH_REPO=<owner>/<repo> selects the repository (default: the current clone's, via gh).
# Needs `gh` with a token that may read/write commit statuses (repo scope locally; the
# workflow token with `statuses: read` in CD).
#
# GATE_STATUS_FAKE_CLOCK=1 (tests only, 11a open item / 11b flaky-test fix): replaces the
# real wall-clock ($SECONDS, sleep) with a fake counter that advances by exactly one whole
# second per poll and never sleeps for real. The --wait loop (deadline arithmetic, poll
# count) runs the identical code path either way — only the time source changes — so a
# test can assert an EXACT, deterministic number of polls instead of depending on real
# scheduling (flaky under load: tests/test_cd_pipeline.py::test_verify_waits_for_a_late_status).
set -euo pipefail

CONTEXT="secrag/gate-full"
POLL_SECONDS="${GATE_STATUS_POLL_SECONDS:-15}"

if [ -n "${GATE_STATUS_FAKE_CLOCK:-}" ]; then
  _fake_clock=0
  _now() { printf '%s' "$_fake_clock"; }
  _sleep() { _fake_clock=$((_fake_clock + 1)); }
else
  _now() { printf '%s' "$SECONDS"; }
  _sleep() { sleep "$1"; }
fi

usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

cmd="${1:-}"
sha="${2:-}"
if [ -z "$cmd" ] || [ -z "$sha" ]; then
  usage >&2
  exit 2
fi
shift 2
wait_s=0 description="scripts/gate.sh --full PASS" creator=""
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) wait_s="$2"; shift ;;
    --description) description="$2"; shift ;;
    --creator) creator="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "gate_status: <sha> must be a full 40-character commit SHA, got '$sha'" >&2
  exit 2
fi
command -v gh >/dev/null || { echo "gate_status: gh not found" >&2; exit 1; }
if [ -z "${GH_REPO:-}" ]; then
  GH_REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)" || exit 1
fi
export GH_REPO
deadline=$(($(_now) + wait_s))

case "$cmd" in
  publish)
    until gh api "repos/$GH_REPO/commits/$sha" --silent >/dev/null 2>&1; do
      if [ "$(_now)" -ge "$deadline" ]; then
        echo "gate_status: $sha is not on GitHub (yet) — $CONTEXT not published"
        exit 3
      fi
      _sleep "$POLL_SECONDS"
    done
    gh api -X POST "repos/$GH_REPO/statuses/$sha" \
      -f state=success -f context="$CONTEXT" -f description="${description:0:140}" \
      --silent
    echo "gate_status: published $CONTEXT=success for $sha ($GH_REPO)"
    ;;
  verify)
    while :; do
      # Newest first; the first entry of our context is its current state.
      latest="$(gh api "repos/$GH_REPO/commits/$sha/statuses?per_page=100" \
        --jq "[.[] | select(.context == \"$CONTEXT\")][0] // {} | \"\(.state // \"\") \(.creator.login // \"\")\"")" \
        || latest=""
      state="${latest%% *}"
      by="${latest#* }"
      if [ "$state" = success ] && { [ -z "$creator" ] || [ "$by" = "$creator" ]; }; then
        echo "gate_status: $CONTEXT=success for $sha (posted by ${by:-?}) — deploy allowed"
        exit 0
      fi
      [ "$(_now)" -lt "$deadline" ] || break
      _sleep "$POLL_SECONDS"
    done
    if [ -z "$state" ]; then
      reason="no $CONTEXT status"
    elif [ "$state" != success ]; then
      reason="$CONTEXT is '$state'"
    else
      reason="$CONTEXT was posted by '$by', not by '$creator'"
    fi
    echo "gate_status: REFUSED — $reason for $sha. Run scripts/gate.sh --full on exactly this commit (it publishes the status)."
    exit 1
    ;;
  *) usage >&2; exit 2 ;;
esac
