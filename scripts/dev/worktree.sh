#!/usr/bin/env bash
# scripts/dev/worktree.sh add|list|remove <block> [<branch>]
#
# One throwaway `git worktree` per concurrent block under ~/proyectos/wt/<block> (Paralelo
# problem #1: agents share one working tree -- a second full clone would duplicate the
# venv/npm caches and the .gate/ seed for nothing). `add`:
#   - creates the worktree (checks out <branch> if it exists, else creates it from HEAD);
#   - symlinks .env / backend/.env from the main tree, the same way the pre-push hook's own
#     temporary worktree does (.githooks/pre-push);
#   - `npm ci` in frontend/, using npm's already-shared, per-user cache (~/.npm by default
#     -- no override needed, every worktree already shares it);
#   - warms the backend venv via scripts/gate.sh's own hash-keyed cache instead of
#     duplicating that logic here: a cheap `--only environment` run resolves it as a side
#     effect (reused unchanged when the worktree's pins match the main venv, built once
#     into the shared cache otherwise -- see gate.sh's resolve_venv/build_venv).
# `remove` refuses when the worktree has uncommitted changes (pass --force to override)
# and always prunes stale worktree metadata afterwards. Both `add` and `remove` refuse any
# computed worktree path that resolves to the main working tree, an ancestor of it, or
# anywhere outside SECRAG_WT_ROOT (DA-PB-1) -- `remove` additionally only ever deletes a
# directory that `git worktree list --porcelain` confirms is a registered (or
# prunable-stale) linked worktree of THIS repo; it never blind-`rm -rf`s on an unconfirmed
# path, and never swallows `git worktree remove`'s stderr.
#
# Usage:
#   scripts/dev/worktree.sh add <block> [<branch>]     branch defaults to the block id
#   scripts/dev/worktree.sh list
#   scripts/dev/worktree.sh remove <block> [--force]
#
# Env: SECRAG_WT_ROOT overrides the worktree root (default ~/proyectos/wt).
set -uo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
MAIN_ROOT="$(cd "$(dirname "$SELF")/../.." && pwd)" || exit 1
WT_ROOT="${SECRAG_WT_ROOT:-$HOME/proyectos/wt}"

usage() { sed -n '2,20p' "$SELF" | sed 's/^# \{0,1\}//'; }

# Resolve $WT_ROOT/$block to an absolute path (via `realpath -m`, so it works even before
# the directory exists) and refuse it when it is the main working tree, an ancestor of the
# main working tree, or outside $WT_ROOT. On success prints the resolved path on stdout.
# On failure prints a FAIL message to stderr and returns 1 -- never silently proceeds.
resolve_wt_path() {
  local block="$1" wt wt_real main_real wt_root_real
  wt="$WT_ROOT/$block"
  wt_real="$(realpath -m -- "$wt")" || { echo "FAIL: cannot resolve $wt" >&2; return 1; }
  main_real="$(realpath -- "$MAIN_ROOT")" || { echo "FAIL: cannot resolve $MAIN_ROOT" >&2; return 1; }
  mkdir -p "$WT_ROOT" || { echo "FAIL: cannot create $WT_ROOT" >&2; return 1; }
  wt_root_real="$(realpath -- "$WT_ROOT")" || { echo "FAIL: cannot resolve $WT_ROOT" >&2; return 1; }

  case "$main_real" in
    "$wt_real")
      echo "FAIL: refusing to use $wt -- it is the main working tree" >&2
      return 1
      ;;
    "$wt_real"/*)
      echo "FAIL: refusing to use $wt -- it is an ancestor of the main working tree" >&2
      return 1
      ;;
  esac

  case "$wt_real" in
    "$wt_root_real"/*) : ;;
    *)
      echo "FAIL: refusing to use $wt_real -- it is outside SECRAG_WT_ROOT ($wt_root_real)" >&2
      return 1
      ;;
  esac

  printf '%s\n' "$wt_real"
}

cmd_add() {
  local block="${1:-}" branch wt
  [ -n "$block" ] || { echo "usage: worktree.sh add <block> [<branch>]" >&2; return 2; }
  branch="${2:-$block}"
  wt="$WT_ROOT/$block"
  resolve_wt_path "$block" >/dev/null || return 1

  if [ -e "$wt" ]; then
    echo "FAIL: $wt already exists (worktree.sh remove $block first)" >&2
    return 1
  fi
  mkdir -p "$WT_ROOT" || return 1

  if git -C "$MAIN_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$MAIN_ROOT" worktree add "$wt" "$branch"
  else
    git -C "$MAIN_ROOT" worktree add -b "$branch" "$wt"
  fi || { echo "FAIL: git worktree add failed" >&2; return 1; }

  for envfile in .env backend/.env; do
    if [ -f "$MAIN_ROOT/$envfile" ] && [ ! -e "$wt/$envfile" ]; then
      ln -s "$MAIN_ROOT/$envfile" "$wt/$envfile"
    fi
  done

  if [ -f "$wt/frontend/package-lock.json" ]; then
    echo "worktree.sh: npm ci (shared cache) in $wt/frontend"
    (cd "$wt/frontend" && PATH="$HOME/.local/bin:$PATH" \
      NODE_OPTIONS=--dns-result-order=ipv4first npm ci --no-audit --no-fund --loglevel=error) \
      || echo "worktree.sh: npm ci failed -- run it by hand before using this worktree" >&2
  fi

  if [ -x "$wt/scripts/gate.sh" ]; then
    echo "worktree.sh: warming the backend venv (gate.sh's own hash cache)"
    GATE_MAIN_ROOT="$MAIN_ROOT" GATE_LOCK_LABEL="worktree-add:$block" \
      bash "$wt/scripts/gate.sh" --only environment \
      || echo "worktree.sh: venv warm-up failed (non-fatal -- the first real gate run retries)" >&2
  fi

  # A short note printed, not written as a file: a file under $wt would show up as an
  # untracked change in `git status`, which `remove`'s uncommitted-changes check (below)
  # would then refuse to remove without --force.
  cat <<NOTE
worktree.sh: $wt ready (branch $branch)
  This directory is a throwaway \`git worktree\`, not a separate clone: it shares this
  repository's git objects and the backend-venv / npm caches. Remove it from the main
  tree with:  scripts/dev/worktree.sh remove $block
NOTE
}

cmd_list() {
  git -C "$MAIN_ROOT" worktree list | awk -v root="$WT_ROOT/" 'index($1, root) == 1'
}

cmd_remove() {
  local block="${1:-}" force=0 wt wt_real
  [ -n "$block" ] || { echo "usage: worktree.sh remove <block> [--force]" >&2; return 2; }
  [ "${2:-}" = "--force" ] && force=1
  wt="$WT_ROOT/$block"

  wt_real="$(resolve_wt_path "$block")" || return 1

  if [ ! -e "$wt" ]; then
    echo "FAIL: no worktree at $wt" >&2
    return 1
  fi
  if [ "$force" -eq 0 ] && [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    echo "FAIL: $wt has uncommitted changes -- commit/stash them or pass --force" >&2
    return 1
  fi

  # Never delete a directory on trust alone: only proceed when `git worktree list
  # --porcelain` (run against the MAIN repo, not $wt itself) confirms $wt_real is a
  # linked worktree it knows about -- registered or stale-but-still-listed
  # ("prunable"). This is the one check that stops a misconfigured/overridden
  # SECRAG_WT_ROOT + block from resolving to an arbitrary directory and having it
  # force-deleted (DA-PB-1).
  if ! git -C "$MAIN_ROOT" worktree list --porcelain | grep -Fqx "worktree $wt_real"; then
    echo "FAIL: $wt_real is not a worktree registered with $MAIN_ROOT -- refusing to delete it" >&2
    return 1
  fi

  # stderr is never swallowed: a real failure (e.g. a lock held by another process)
  # must be visible. Only fall back to rm -rf for the specific case git itself still
  # considers this path a worktree of this repo (checked above) but `remove` failed
  # on stale metadata -- not for every failure.
  if ! git -C "$MAIN_ROOT" worktree remove --force "$wt"; then
    echo "worktree.sh: git worktree remove failed for a confirmed worktree ($wt_real) -- falling back to rm -rf" >&2
    rm -rf "$wt" || { echo "FAIL: rm -rf $wt also failed" >&2; return 1; }
  fi
  git -C "$MAIN_ROOT" worktree prune
  echo "worktree.sh: removed $wt"
}

case "${1:-}" in
  add) shift; cmd_add "$@" ;;
  list) cmd_list ;;
  remove) shift; cmd_remove "$@" ;;
  -h | --help | "") usage ;;
  *) echo "unknown command: $1" >&2; usage >&2; exit 2 ;;
esac
