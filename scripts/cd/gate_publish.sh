#!/usr/bin/env bash
# After a gate run: publish `secrag/gate-full` = success for the checked SHA, or say why not
# (D-2026-09-27-7 b; decision logic split out of gate.sh so it can be tested, DA-C-4).
#
#   gate_publish.sh --mode <fast|full|only|seed> --result <PASS|FAIL> --start-sha <sha>
#                   --start-dirty <0|1> --total <seconds> --state-dir <dir>
#
# Run from the checked tree (gate.sh: REPO_ROOT). Publishes ONLY when all hold:
#   mode full · result PASS · HEAD is still --start-sha · the tree was clean at the start
#   (--start-dirty 0) and is clean now · SECRAG_GATE_PUBLISH is unset or 1 · gh works.
# Anything else → a "NOT published" line (fast/only/seed: silent) and exit 0; this script
# never changes the gate result. Under the pre-push hook the commit is not on GitHub yet
# (the gate runs before the push lands): gate_status.sh publish returns 3, and a detached
# publisher — a copy of gate_status.sh in --state-dir, outside the temporary worktree —
# polls for up to 15 min and posts it (log: <state-dir>/publish-status.log). The log line
# records the publisher's own pid (`[pid=…]`) so a LATER run can tell a publisher that is
# still polling apart from one that died without a trace (see `_warn_stale_publishers`,
# 11b block D: a detached publisher can be killed outright — machine sleep, a Docker
# Desktop/WSL restart, a reboot — between its "waiting" line and ever posting or timing
# out, with no error and no further log line; that is an OS/VM-level interruption, not a
# bug in this script's decision logic, but it must not stay silently buried in a log file
# nobody checks automatically).
set -uo pipefail

# Scans the log for a PRIOR "waiting for <sha> ... [pid=<pid>]" line (any sha but the one
# this run is about to handle) that is still unresolved: not mentioned again anywhere else
# in the log (a "published" line or a timeout's "not published" line both count as locally
# resolved) and whose recorded pid is no longer running. Before warning, it also checks
# LIVE whether that sha was published some other way (DA-11bB-1 found a prior case of
# exactly that) — only a dead, still-unpublished sha gets the warning, printed to THIS
# run's own stdout (so it is seen) and appended to the log.
_warn_stale_publishers() {
  local log="$1" repo="$2" current_sha="$3"
  local gate_status_copy="${log%/*}/gate_status.sh" stale_sha stale_pid msg seen=""
  [ -f "$log" ] || return 0
  while IFS=' ' read -r stale_sha stale_pid; do
    [ -n "$stale_sha" ] || continue
    [ "$stale_sha" = "$current_sha" ] && continue
    # One decision per sha per run, even if it has several "waiting" lines (e.g. a
    # --full retried on the same still-unresolved commit).
    case " $seen " in *" $stale_sha "*) continue ;; esac
    seen="$seen $stale_sha"
    # DA-11bD-1: "resolved" must come from a RESOLUTION-shaped line for this sha — a
    # successful publish, or the --wait timeout's "not on GitHub (yet)" line — never
    # from a raw occurrence count. Two unresolved "waiting for <sha>" lines (both
    # publishers dead without posting) must NOT be read as "resolved" just because the
    # sha appears twice — that was the exact bug the old `grep -c ... -gt 1` check had.
    if grep -qF "published secrag/gate-full=success for $stale_sha" "$log" \
      || grep -qF "$stale_sha is not on GitHub (yet)" "$log"; then
      continue # resolved
    fi
    if [ -n "$stale_pid" ] && kill -0 "$stale_pid" 2>/dev/null; then
      continue # still polling — not stale
    fi
    if GH_REPO="$repo" bash "$gate_status_copy" verify "$stale_sha" >/dev/null 2>&1; then
      continue # published some other way since — not a problem after all
    fi
    msg="gate status: WARNING — an earlier detached publisher for $stale_sha never posted secrag/gate-full and its process is gone (no timeout message either): most likely an OS/VM-level interruption (sleep, a Docker Desktop/WSL restart, a reboot), not a bug here. If $stale_sha is still relevant and was pushed, re-run scripts/gate.sh --full on exactly that commit to re-publish — never post the status by hand."
    echo "$msg"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $msg" >>"$log"
  done < <(sed -n 's/.*waiting for \([0-9a-f]\{40\}\) on .*\[pid=\([0-9]*\)\].*/\1 \2/p' "$log")
}

mode="" result="" sha="" start_dirty="" total="" state_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) mode="$2"; shift ;;
    --result) result="$2"; shift ;;
    --start-sha) sha="$2"; shift ;;
    --start-dirty) start_dirty="$2"; shift ;;
    --total) total="$2"; shift ;;
    --state-dir) state_dir="$2"; shift ;;
    *) echo "gate_publish: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$state_dir" ] || { echo "gate_publish: --state-dir is required" >&2; exit 2; }
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

[ "$mode" = full ] || exit 0
if [ "$result" != PASS ]; then
  echo "gate status: NOT published — the gate did not pass"
  exit 0
fi
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || [ "$(git rev-parse HEAD 2>/dev/null)" != "$sha" ]; then
  echo "gate status: NOT published — HEAD moved during the run (started at ${sha:-?}), so this PASS describes no single commit"
  exit 0
fi
if [ "$start_dirty" != 0 ]; then
  echo "gate status: NOT published — the tree had uncommitted or untracked changes when the gate started"
  exit 0
fi
if [ "${SECRAG_GATE_PUBLISH:-1}" != 1 ]; then
  echo "gate status: not published (SECRAG_GATE_PUBLISH=${SECRAG_GATE_PUBLISH})"
  exit 0
fi
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "gate status: NOT published — the tree has uncommitted or untracked changes, so this PASS does not describe $sha"
  exit 0
fi
command -v gh >/dev/null || { echo "gate status: NOT published — gh not found (CD will refuse $sha)"; exit 0; }
repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
[ -n "$repo" ] || { echo "gate status: NOT published — gh cannot resolve the GitHub repository (CD will refuse $sha)"; exit 0; }

desc="scripts/gate.sh --full PASS in ${total}s"
GH_REPO="$repo" bash "$here/gate_status.sh" publish "$sha" --description "$desc"
rc=$?
[ "$rc" -eq 0 ] && exit 0
if [ "$rc" -ne 3 ]; then
  echo "gate status: NOT published (gh error) — publish by hand: GH_REPO=$repo bash scripts/cd/gate_status.sh publish $sha"
  exit 0
fi
mkdir -p "$state_dir" || exit 0
cp -f "$here/gate_status.sh" "$state_dir/gate_status.sh" || exit 0
_warn_stale_publishers "$state_dir/publish-status.log" "$repo" "$sha"
(
  cd "$state_dir" || exit 0
  # Detached (own session, no inherited stdio) so git does not wait for it. The pid is
  # recorded so a LATER run can tell "still polling" apart from "died without a trace"
  # (_warn_stale_publishers above).
  GH_REPO="$repo" setsid nohup bash "$state_dir/gate_status.sh" publish "$sha" --wait 900 \
    --description "$desc" </dev/null >>publish-status.log 2>&1 &
  bgpid=$!
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) waiting for $sha on $repo (up to 900s) [pid=$bgpid]" >>publish-status.log
)
echo "gate status: $sha is not on GitHub yet — a background publisher posts secrag/gate-full once the push lands (up to 15 min; log: $state_dir/publish-status.log)"
echo "gate status: after the push, check: bash scripts/cd/gate_status.sh verify $sha --wait 300 --creator <owner>"
exit 0
