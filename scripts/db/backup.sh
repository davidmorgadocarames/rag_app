#!/usr/bin/env bash
# Encrypted database backup (T11.2.10): pg_dump AS secrag_backup (read-only role) → age
# (PUBLIC key only) → a local file or a new Blob. The private key never touches this script,
# the Job or Azure (R4-1: password manager + offline copy; only the owner restores).
#
#   scripts/db/backup.sh --file [--dir DIR]   local file mode: DIR (default $BACKUP_DIR or
#                                             ~/secrag-db-backups; mode 0700; never inside a
#                                             git work tree), then files older than the
#                                             retention are deleted
#   scripts/db/backup.sh --blob               Blob mode (the backup Job): upload to
#                                             backups/secrag-<ts>.dump.age with the Job's
#                                             managed identity, then the oldest-blob check
#   scripts/db/backup.sh --retention-days     print the retention (X9) and exit
#
# Environment:
#   DATABASE_URL           the secrag_backup connection (postgresql[+psycopg]://…); the
#                          script refuses any other role
#   BACKUP_AGE_RECIPIENT   the age PUBLIC key (age1…); or --recipient-file FILE (one per line);
#                          default file: deploy/backup/age-recipient.txt when it exists
#   Blob mode also: BACKUP_STORAGE_ACCOUNT, BACKUP_CONTAINER, AZURE_CLIENT_ID (rag_app.backup_blob)
#
# Retention: BACKUP_RETENTION_DAYS in backend/src/rag_app/retention.py (X9): a file
# is removed when the time in its NAME is more than that many days ago. Blob mode relies on
# the Storage lifecycle rule (12 days on backups/) and FAILS when the oldest blob is older.
set -euo pipefail

# shellcheck source=scripts/db/backup_lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup_lib.sh"

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

mode=""
dir="${BACKUP_DIR:-$HOME/secrag-db-backups}"
recipient_file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --file) mode="file" ;;
    --blob) mode="blob" ;;
    --dir) shift; dir="${1:?--dir needs a directory}" ;;
    --recipient-file) shift; recipient_file="${1:?--recipient-file needs a file}" ;;
    --retention-days) retention_days; exit ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
[ -n "$mode" ] || { usage >&2; exit 2; }
days="$(retention_days)"

# --- recipients (public keys only) --------------------------------------------------------
recipients=()
if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  recipients+=("$BACKUP_AGE_RECIPIENT")
else
  [ -n "$recipient_file" ] || recipient_file="$SECRAG_REPO_ROOT/deploy/backup/age-recipient.txt"
  [ -f "$recipient_file" ] \
    || die "no age recipient: set BACKUP_AGE_RECIPIENT or pass --recipient-file (the PUBLIC key)"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(tr -d '[:space:]' <<<"$line")"
    [ -n "$line" ] && recipients+=("$line")
  done <"$recipient_file"
fi
[ "${#recipients[@]}" -gt 0 ] || die "no age recipient found"
age_args=()
for r in "${recipients[@]}"; do
  [[ "$r" =~ $AGE_RECIPIENT_RE ]] \
    || die "not an age public key (age1…) — never give this script a private key"
  age_args+=(-r "$r")
done

# --- source database (secrag_backup only) -------------------------------------------------
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL (the secrag_backup connection) is not set"
pg_env_from_url "$DATABASE_URL"
[ "$PGUSER" = secrag_backup ] \
  || die "refusing to dump as '$PGUSER': backups run as the read-only role secrag_backup"
command -v pg_dump >/dev/null || die "pg_dump (16) not found"
command -v age >/dev/null || die "age not found"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
name="secrag-$ts.dump.age"
echo "backup: pg_dump of $PGDATABASE@$PGHOST as $PGUSER → age (${#recipients[@]} recipient(s)) → $mode"

if [ "$mode" = blob ]; then
  py="$(secrag_python)"
  pg_dump -w -Fc | age "${age_args[@]}" | "$py" -m rag_app.backup_blob upload --name "backups/$name"
  "$py" -m rag_app.backup_blob check
  exit 0
fi

# --- file mode ----------------------------------------------------------------------------
if inside_git_work_tree "$dir"; then
  die "refusing $dir: backups never go inside a git work tree"
fi
mkdir -p -- "$dir"
chmod 700 -- "$dir"
partial="$dir/.$name.partial"
trap 'rm -f -- "$partial"' EXIT
(
  umask 077
  pg_dump -w -Fc | age "${age_args[@]}" >"$partial"
)
is_age_file "$partial" || die "the output is not an age file — nothing kept"
chmod 600 -- "$partial"
mv -f -- "$partial" "$dir/$name"
trap - EXIT
echo "backup: wrote $dir/$name ($(stat -c %s "$dir/$name") bytes, age-encrypted)"
echo "backup: retention $days days (X9) in $dir"
prune_by_name "$dir" "$DUMP_NAME_RE" "$days"
echo "backup: $(find "$dir" -maxdepth 1 -type f -name 'secrag-*.dump.age' | wc -l) dump(s) kept"
