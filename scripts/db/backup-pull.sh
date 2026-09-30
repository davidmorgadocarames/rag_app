#!/usr/bin/env bash
# Weekly local copy of the latest encrypted Blob dump (T11.2.13) — run by the owner in WSL
# (Linux `az`, logged in; the owner's Storage Blob Data Reader role — shared-key access is
# disabled, so every call uses --auth-mode login). Schedule it weekly (README "Backups").
#
#   BACKUP_STORAGE_ACCOUNT=<account> scripts/db/backup-pull.sh [--dir DIR]
#
# - downloads the newest backups/secrag-<ts>.dump.age (still age-encrypted: nothing is
#   decrypted here) into DIR (default ~/secrag-db-backups/azure, mode 0700, never inside a
#   git work tree), unless it is already there;
# - downloads the newest tombstone export tombstones/tombstones-<ts>.jsonl into DIR/tombstones
#   (restore.sh --tombstones-dir; opaque ids only);
# - deletes local dumps and tombstone exports older than the retention (X9 constant, read from
#   backend/src/rag_app/retention.py; the newest tombstone export is always kept).
# BACKUP_CONTAINER overrides the container (default secrag-backups).
set -euo pipefail

# shellcheck source=scripts/db/backup_lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup_lib.sh"

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

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
if inside_git_work_tree "$dir"; then
  die "refusing $dir: backups never go inside a git work tree"
fi
mkdir -p -- "$dir/tombstones"
chmod 700 -- "$dir" "$dir/tombstones"

azs() { az storage blob "$@" --account-name "$account" --container-name "$container" --auth-mode login --only-show-errors; }

# latest <prefix> <basename regex>: newest blob name under the prefix (names sort by time).
latest() {
  azs list --prefix "$1" --query '[].name' -o tsv \
    | tr -d '\r' | { grep -E "^$1${2:1}" || true; } | sort | tail -n 1
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

dump="$(latest backups/ "$DUMP_NAME_RE")"
[ -n "$dump" ] || die "no backup blob under backups/ in $account/$container"
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

export_name="$(latest tombstones/ "$TOMBSTONE_NAME_RE")"
if [ -n "$export_name" ]; then
  fetch "$export_name" "$dir/tombstones/$(basename "$export_name")"
  echo "backup-pull: tombstone export $(basename "$export_name")"
else
  echo "backup-pull: no tombstone export yet under tombstones/ (the purge Job writes it)"
fi

echo "backup-pull: retention $days days (X9)"
prune_by_name "$dir" "$DUMP_NAME_RE" "$days"
prune_by_name "$dir/tombstones" "$TOMBSTONE_NAME_RE" "$days" keep-newest
echo "backup-pull: $(find "$dir" -maxdepth 1 -type f -name 'secrag-*.dump.age' | wc -l) local dump(s) in $dir"
