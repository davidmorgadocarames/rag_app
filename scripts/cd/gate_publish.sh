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
# polls for up to 15 min and posts it (log: <state-dir>/publish-status.log).
set -uo pipefail

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
(
  cd "$state_dir" || exit 0
  # Detached (own session, no inherited stdio) so git does not wait for it.
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) waiting for $sha on $repo (up to 900s)" >>publish-status.log
  GH_REPO="$repo" setsid nohup bash "$state_dir/gate_status.sh" publish "$sha" --wait 900 \
    --description "$desc" </dev/null >>publish-status.log 2>&1 &
)
echo "gate status: $sha is not on GitHub yet — a background publisher posts secrag/gate-full once the push lands (up to 15 min; log: $state_dir/publish-status.log)"
echo "gate status: after the push, check: bash scripts/cd/gate_status.sh verify $sha --wait 300 --creator <owner>"
exit 0
