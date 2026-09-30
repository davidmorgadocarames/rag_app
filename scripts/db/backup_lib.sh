#!/usr/bin/env bash
# Shared helpers for scripts/db/backup.sh, restore.sh, backup-pull.sh and backup_drill.sh
# (T11.2.10, T11.2.13). Sourced, never run. Works from the repository and from the slim jobs
# image (/app/scripts/db, /app/src).
#
# - retention_days: the X9 constant, read from backend/src/rag_app/retention.py (the single
#   source; `BACKUP_RETENTION_DAYS = <n>` on its own line).
# - Dump files are named secrag-<yyyymmddThhmmssZ>.dump.age; their age is the time in the
#   NAME (when the dump was taken), so a copied or touched file keeps its real age.
# - pg_env_from_url: DATABASE_URL-style URL → libpq variables (the password never goes on a
#   command line).

# shellcheck disable=SC2034  # variables used by the scripts that source this file
SECRAG_DB_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SECRAG_REPO_ROOT="$(cd "$SECRAG_DB_DIR/../.." && pwd)"
DUMP_NAME_RE='^secrag-([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z\.dump\.age$'
TOMBSTONE_NAME_RE='^tombstones-([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z\.jsonl$'
AGE_RECIPIENT_RE='^age1[02-9ac-hj-np-z]{58}$'
AGE_HEADER='age-encryption.org/v1'

die() {
  echo "$(basename "$0"): $*" >&2
  exit 1
}

retention_file() {
  local f
  for f in "$SECRAG_REPO_ROOT/backend/src/rag_app/retention.py" \
           "$SECRAG_REPO_ROOT/src/rag_app/retention.py"; do
    [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

retention_days() {
  local f v
  f="$(retention_file)" || { echo "retention.py not found next to $SECRAG_DB_DIR" >&2; return 1; }
  v="$(sed -n 's/^BACKUP_RETENTION_DAYS = \([0-9][0-9]*\)$/\1/p' "$f")"
  [ -n "$v" ] || { echo "no 'BACKUP_RETENTION_DAYS = <n>' line in $f" >&2; return 1; }
  echo "$v"
}

# secrag_python: the interpreter with rag_app (SECRAG_PYTHON, the backend venv, or the jobs
# image's python) — exports PYTHONPATH for a repository checkout.
secrag_python() {
  if [ -n "${SECRAG_PYTHON:-}" ]; then
    echo "$SECRAG_PYTHON"
  elif [ -x "$SECRAG_REPO_ROOT/backend/.venv/bin/python" ]; then
    echo "$SECRAG_REPO_ROOT/backend/.venv/bin/python"
  else
    command -v python3 || command -v python
  fi
}
if [ -d "$SECRAG_REPO_ROOT/backend/src" ]; then
  export PYTHONPATH="$SECRAG_REPO_ROOT/backend/src${PYTHONPATH:+:$PYTHONPATH}"
fi

# Clock skew allowed for a name time in the future, and the age after which a leftover
# .<name>.partial file (a SIGKILLed backup or download) is removed (DA-F-3).
FUTURE_SKEW_SECONDS=86400
PARTIAL_MAX_AGE_SECONDS=86400

# name_epoch <basename> <regex>: seconds since the epoch of the timestamp in the name. Fails
# when the name does not match, or when the date is not a real one (month 13, Feb 31, 24:00 —
# the time must format back to exactly the same digits).
name_epoch() {
  [[ "$1" =~ $2 ]] || return 1
  local m=("${BASH_REMATCH[@]}") epoch
  epoch="$(date -u -d "${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}" +%s 2>/dev/null)" \
    || return 1
  [ "$(date -u -d "@$epoch" +%Y%m%d%H%M%S)" = "${m[1]}${m[2]}${m[3]}${m[4]}${m[5]}${m[6]}" ] \
    || return 1
  echo "$epoch"
}

# prune_by_name <dir> <regex> <days> [keep-newest]: delete matching files whose name time is
# more than <days> days old. Prints one line per removed file; with keep-newest the newest
# match is never removed. Files that do not match the regex are never touched, except
# leftover partial files: .<matching name>.partial older than PARTIAL_MAX_AGE_SECONDS (by
# mtime) are removed. A matching name with an invalid date, or a time more than
# FUTURE_SKEW_SECONDS in the future, is kept but REPORTED on stderr and the function returns
# 1 after the sweep (it would otherwise be kept forever, silently — DA-F-3).
prune_by_name() {
  local dir="$1" re="$2" days="$3" keep_newest="${4:-}" now f base inner epoch newest="" bad=0
  now="$(date -u +%s)"
  if [ -n "$keep_newest" ]; then
    newest="$(find "$dir" -maxdepth 1 -type f -printf '%f\n' | { grep -E "$re" || true; } \
      | sort | tail -n 1)"
  fi
  for f in "$dir"/* "$dir"/.*.partial; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    base="$(basename "$f")"
    if [[ "$base" == .*.partial ]]; then
      inner="${base#.}"
      inner="${inner%.partial}"
      [[ "$inner" =~ $re ]] || continue
      if [ $((now - $(stat -c %Y -- "$f"))) -gt "$PARTIAL_MAX_AGE_SECONDS" ]; then
        rm -f -- "$f"
        echo "  removed $base (stale partial file)"
      fi
      continue
    fi
    [[ "$base" =~ $re ]] || continue
    if ! epoch="$(name_epoch "$base" "$re")"; then
      echo "  WARNING: $base has an invalid date in its name — kept; check and remove it by hand" >&2
      bad=1
      continue
    fi
    if [ $((epoch - now)) -gt "$FUTURE_SKEW_SECONDS" ]; then
      echo "  WARNING: $base is dated in the future — kept; check the clock / remove it by hand" >&2
      bad=1
      continue
    fi
    [ "$base" = "$newest" ] && continue
    if [ $((now - epoch)) -gt $((days * 86400)) ]; then
      rm -f -- "$f"
      echo "  removed $base (older than $days days)"
    fi
  done
  return "$bad"
}

# is_age_file <file>: the file starts with the age header line.
is_age_file() { [ "$(head -c ${#AGE_HEADER} -- "$1" 2>/dev/null)" = "$AGE_HEADER" ]; }

# inside_git_work_tree <path>: the path (or its nearest existing parent) is in a git work tree.
inside_git_work_tree() {
  local p="$1"
  while [ ! -e "$p" ]; do p="$(dirname "$p")"; done
  [ -d "$p" ] || p="$(dirname "$p")"
  [ "$(git -C "$p" rev-parse --is-inside-work-tree 2>/dev/null)" = true ]
}

# pg_env_from_url <url>: export PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE[/PGSSLMODE] from a
# postgresql[+driver]:// URL (after clearing any inherited libpq target variables).
pg_env_from_url() {
  local kv
  unset PGHOST PGHOSTADDR PGPORT PGUSER PGPASSWORD PGDATABASE PGSSLMODE PGSERVICE PGSERVICEFILE
  while IFS= read -r -d '' kv; do
    export "${kv?}"
  done < <(SECRAG_URL="$1" python3 -c '
import os, sys, urllib.parse as u
p = u.urlsplit(os.environ["SECRAG_URL"])
if not p.scheme.startswith("postgresql"):
    sys.exit("not a postgresql:// URL")
q = dict(u.parse_qsl(p.query))
env = {"PGHOST": p.hostname or "", "PGPORT": str(p.port or 5432),
       "PGUSER": u.unquote(p.username or ""), "PGPASSWORD": u.unquote(p.password or ""),
       "PGDATABASE": u.unquote(p.path.lstrip("/")), "PGSSLMODE": q.get("sslmode", "")}
for k, v in env.items():
    if v:
        sys.stdout.write(k + "=" + v + "\0")
')
  [ -n "${PGHOST:-}" ] && [ -n "${PGDATABASE:-}" ] || die "cannot parse the database URL"
}
