#!/usr/bin/env bash
# Weekly local copy of the latest encrypted Blob dump (T11.2.13) — run by the owner in WSL
# (Linux `az`, logged in; the owner's Storage Blob Data Reader role — shared-key access is
# disabled, so every call uses --auth-mode login). Schedule it weekly (README "Backups").
#
#   BACKUP_STORAGE_ACCOUNT=<account> scripts/db/backup-pull.sh [--dir DIR]
#
# "Newest" is by the time in the NAME, among valid names only: a name with an invalid date or
# dated in the future is never picked, it is reported and the run exits 1 (DA-G1-3).
# - downloads the newest backups/secrag-<ts>.dump.age (still age-encrypted: nothing is
#   decrypted here) into DIR (default ~/secrag-db-backups/azure, mode 0700, never inside a
#   git work tree), unless it is already there;
# - downloads the newest tombstone export tombstones/tombstones-<ts>.jsonl into DIR/tombstones
#   (restore.sh --tombstones-dir; opaque ids only);
# - deletes local dumps older than the retention (X9 constant) and tombstone exports older
#   than TOMBSTONE_EXPORT_RETENTION_DAYS (30; the newest valid export is always kept, and none
#   is deleted while a name cannot be judged) — both from backend/src/rag_app/retention.py.
#   A non-zero exit after the download means: read the WARNING line (a name to check by hand).
# BACKUP_CONTAINER overrides the container (default secrag-backups).
set -euo pipefail

# shellcheck source=scripts/db/backup_lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup_lib.sh"

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

dir="${BACKUP_PULL_DIR:-$HOME/secrag-db-backups/azure}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) shift; dir="${1:?--dir needs a directory}" ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

account="${BACKUP_STORAGE_ACCOUNT:-}"
container="${BACKUP_CONTAINER:-secrag-backups}"
[[ "$account" =~ ^[a-z0-9]{3,24}$ ]] || die "set BACKUP_STORAGE_ACCOUNT (the storage account name)"
command -v az >/dev/null || die "az (the Linux Azure CLI) not found"
days="$(retention_days)"
tdays="$(tombstone_retention_days)"
if inside_git_work_tree "$dir"; then
  die "refusing $dir: backups never go inside a git work tree"
fi
mkdir -p -- "$dir/tombstones"
chmod 700 -- "$dir" "$dir/tombstones"

azs() { az storage blob "$@" --account-name "$account" --container-name "$container" --auth-mode login --only-show-errors; }

flagged=0
# latest <prefix> <basename regex>: the blob under the prefix whose NAME time is the newest
# VALID one; a name that cannot be judged (invalid date, dated in the future) is never picked
# — it is reported and the run exits 1 at the end (DA-G1-3). Returns 1 after such a report,
# 2 when the list call itself fails; an empty result means nothing valid is there.
latest() {
  local names
  names="$(azs list --prefix "$1" --query '[].name' -o tsv)" || return 2
  newest_valid_name "$2" "$1" <<<"$names"
}
# pick <prefix> <regex>: latest, with a failed list call fatal and a report remembered.
picked=""
pick() {
  local rc=0
  picked="$(latest "$1" "$2")" || rc=$?
  [ "$rc" -le 1 ] || die "cannot list $1 in $account/$container"
  [ "$rc" = 0 ] || flagged=1
}

# fetch <blob name> <target file>: download to a partial file, then rename.
fetch() {
  local partial
  partial="$(dirname "$2")/.$(basename "$2").partial"
  rm -f -- "$partial"
  (umask 077; azs download --name "$1" --file "$partial" >/dev/null)
  mv -f -- "$partial" "$2"
  chmod 600 -- "$2"
}

pick backups/ "$DUMP_NAME_RE"
dump="$picked"
[ -n "$dump" ] || die "no valid backup blob under backups/ in $account/$container"
base="$(basename "$dump")"
if [ -f "$dir/$base" ]; then
  echo "backup-pull: $base already here"
else
  fetch "$dump" "$dir/$base"
  if ! is_age_file "$dir/$base"; then
    rm -f -- "$dir/$base"
    die "$base is not an age-encrypted file — removed"
  fi
  echo "backup-pull: downloaded $base ($(stat -c %s "$dir/$base") bytes, age-encrypted)"
fi

pick tombstones/ "$TOMBSTONE_NAME_RE"
export_name="$picked"
if [ -n "$export_name" ]; then
  fetch "$export_name" "$dir/tombstones/$(basename "$export_name")"
  echo "backup-pull: tombstone export $(basename "$export_name")"
else
  echo "backup-pull: no tombstone export yet under tombstones/ (the purge Job writes it)"
fi

echo "backup-pull: retention $days days (X9), tombstone exports $tdays days"
prune_by_name "$dir" "$DUMP_NAME_RE" "$days" || flagged=1
prune_by_name "$dir/tombstones" "$TOMBSTONE_NAME_RE" "$tdays" keep-newest || flagged=1
echo "backup-pull: $(find "$dir" -maxdepth 1 -type f -name 'secrag-*.dump.age' | wc -l) local dump(s) in $dir"
[ "$flagged" = 0 ] || die "found names it cannot judge (WARNING above) — check them"
