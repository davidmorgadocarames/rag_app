#!/usr/bin/env bash
# Backup drill (T11.2.10; gate step `backup-drill`): backup → erase a test user → restore →
# the user stays erased; decrypt with the OFFLINE copy of the key; files older than the retention (X9) removed.
#
#   DRILL_ADMIN_URL=postgresql://<superuser>:<pw>@127.0.0.1:<port>/postgres \
#   SECRAG_BACKUP_PASSWORD=<secrag_backup password> scripts/db/backup_drill.sh
#
# Runs ONLY on a local throwaway server (the gate project): the admin URL must be loopback and
# not port 5432 (the development database). Everything it creates is thrown away: databases
# secrag_drill_*, a mktemp work directory, and THROWAWAY age keypairs (never the real backup
# key; key files are shredded at the end and nothing prints a private key).
#
#   1. fresh database, db/roles.sql, alembic upgrade head, two synthetic accounts
#      (keep / erase) with keys, a conversation and messages
#   2. throwaway keypair; its private key is copied to an "offline" folder (the password
#      manager / offline copy of R4-1) and the working copy is shredded
#   3. backup.sh --file as secrag_backup into a folder that already holds a 15-day-old dump,
#      a 13-day-old dump and an unrelated file → only the 15-day-old one is removed
#   4. erase the test account (the app's path) and export the tombstones (the purger's export)
#   5. restore.sh with the OFFLINE key into a new empty database → the erased account stays
#      erased (tombstone "done", no key/conversation/message), the kept one is intact
#   6. controls: without the tombstone export the erased account comes back (the export is
#      what keeps it erased); a wrong key restores nothing; a non-empty target is refused
set -euo pipefail

# shellcheck source=scripts/db/backup_lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup_lib.sh"

[ -n "${DRILL_ADMIN_URL:-}" ] || die "DRILL_ADMIN_URL is not set"
[ -n "${SECRAG_BACKUP_PASSWORD:-}" ] || die "SECRAG_BACKUP_PASSWORD is not set"
for tool in age age-keygen pg_dump pg_restore psql jq shred; do
  command -v "$tool" >/dev/null || die "$tool not found"
done
py="$(secrag_python)"

pg_env_from_url "$DRILL_ADMIN_URL"
case "$PGHOST" in 127.0.0.1 | localhost | ::1) ;; *) die "refusing non-local host $PGHOST" ;; esac
[ "$PGPORT" != 5432 ] || die "refusing port 5432: that is the development database"
admin_user="$PGUSER" admin_pw="$PGPASSWORD" host="$PGHOST" port="$PGPORT"
url_for() { echo "postgresql://$1:$2@$host:$port/$3"; }
admin_psql() { PGDATABASE=postgres psql -X -w -q -v ON_ERROR_STOP=1 "$@"; }

suffix="$(openssl rand -hex 4)"
src="secrag_drill_src_$suffix" rst="secrag_drill_rst_$suffix"
ctl="secrag_drill_ctl_$suffix" neg="secrag_drill_neg_$suffix"
work="$(mktemp -d "${TMPDIR:-/tmp}/secrag-backup-drill.XXXXXX")"
chmod 700 "$work"
cleanup() {
  local db
  for db in "$src" "$rst" "$ctl" "$neg"; do
    admin_psql -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" >/dev/null 2>&1 || true
  done
  find "$work" -type f -name '*.key' -exec shred -u {} + 2>/dev/null || true
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "  ok: $*"; }
fail() { echo "  FAIL: $*"; exit 1; }

# --- 1. source database with two accounts -------------------------------------------------
for db in "$src" "$rst" "$ctl" "$neg"; do admin_psql -c "CREATE DATABASE $db"; done
# DA-F-5: no password on any command line — the URL carries none; the admin password reaches
# apply_roles.sh (and its psql) only through the exported PGPASSWORD of pg_env_from_url.
PGPASSWORD="$admin_pw" bash "$SECRAG_DB_DIR/apply_roles.sh" "postgresql://$admin_user@$host:$port/$src" \
  | sed 's/^/  /'
(cd "$SECRAG_REPO_ROOT/backend" \
  && DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$src")" "$py" -m alembic upgrade head 2>&1 \
  | sed 's/^/  /')
ids="$(DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$src")" "$py" -m rag_app.devtools.backup_drill seed)"
keep="$(jq -r .keep <<<"$ids")" erase="$(jq -r .erase <<<"$ids")"
ok "source database $src: 2 synthetic accounts (keep, erase)"

# --- 2. throwaway keypair; decrypt only with the offline copy -----------------------------
mkdir -m 700 "$work/keys" "$work/offline-copy" "$work/backups" "$work/tombstones" "$work/empty"
age-keygen -o "$work/keys/backup.key" 2>/dev/null
recipient="$(age-keygen -y "$work/keys/backup.key")"
install -m 600 "$work/keys/backup.key" "$work/offline-copy/backup.key"
shred -u "$work/keys/backup.key"
age-keygen -o "$work/keys/wrong.key" 2>/dev/null
ok "throwaway keypair; private key only in the offline copy (working copy shredded)"

# --- 3. backup (file mode) + retention ----------------------------------------------------
days="$(retention_days)"
old="secrag-$(date -u -d "$((days + 1)) days ago" +%Y%m%dT%H%M%SZ).dump.age"
recent="secrag-$(date -u -d "$((days - 1)) days ago" +%Y%m%dT%H%M%SZ).dump.age"
for f in "$old" "$recent"; do printf '%s\nplanted\n' "$AGE_HEADER" >"$work/backups/$f"; done
echo "not a dump" >"$work/backups/notes.txt"
DATABASE_URL="$(url_for secrag_backup "$SECRAG_BACKUP_PASSWORD" "$src")" BACKUP_AGE_RECIPIENT="$recipient" \
  bash "$SECRAG_DB_DIR/backup.sh" --file --dir "$work/backups" | sed 's/^/  /'
dump="$(find "$work/backups" -maxdepth 1 -name 'secrag-*.dump.age' ! -name "$old" ! -name "$recent" | head -n 1)"
{ [ -n "$dump" ] && is_age_file "$dump"; } || fail "no new age-encrypted dump"
[ "$(stat -c %a "$dump")" = 600 ] || fail "the dump is not mode 600"
[ ! -e "$work/backups/$old" ] || fail "the dump older than $days days was not removed"
[ -e "$work/backups/$recent" ] || fail "the $((days - 1))-day-old dump was removed"
[ -e "$work/backups/notes.txt" ] || fail "an unrelated file was removed"
if age -d -i "$work/keys/wrong.key" "$dump" >/dev/null 2>&1; then fail "a wrong key decrypts the dump"; fi
ok "backup as secrag_backup: $(basename "$dump") (age); > $days days removed, $((days - 1)) days kept, other files untouched"

# --- 4. erase the test user + export the tombstones ---------------------------------------
DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$src")" "$py" -m rag_app.devtools.backup_drill erase --user "$erase" >/dev/null
DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$src")" "$py" -m rag_app.tombstones export --dir "$work/tombstones" | sed 's/^/  /'
ok "erased the test account after the backup; tombstones exported"

state() { DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$1")" "$py" -m rag_app.devtools.backup_drill state --user "$keep" --user "$erase"; }
restore_into() { # restore_into <db> <tombstone dir> <identity>
  RESTORE_DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$1")" \
    bash "$SECRAG_DB_DIR/restore.sh" --dump "$dump" --identity "$3" --tombstones-dir "$2"
}

# --- 5. restore with the offline key: the user stays erased -------------------------------
restore_into "$rst" "$work/tombstones" "$work/offline-copy/backup.key" 2>&1 | sed 's/^/  /'
s="$(state "$rst")"
jq -e --arg k "$keep" '.[$k] | .user and .key and .conversations == 1 and .messages == 2 and .tombstone == null' <<<"$s" >/dev/null \
  || fail "the kept account is not intact after the restore: $(jq -c --arg k "$keep" '.[$k]' <<<"$s")"
jq -e --arg e "$erase" '.[$e] | (.user | not) and (.key | not) and .conversations == 0 and .messages == 0 and .tombstone == "done"' <<<"$s" >/dev/null \
  || fail "the erased account came back after the restore: $(jq -c --arg e "$erase" '.[$e]' <<<"$s")"
ok "restore (offline key) into $rst: erased account stays erased (tombstone done, no key/conversation/message); kept account intact"

# --- 6. controls --------------------------------------------------------------------------
restore_into "$ctl" "$work/empty" "$work/offline-copy/backup.key" >"$work/ctl.log" 2>&1 \
  || fail "control restore failed: $(tail -n 1 "$work/ctl.log")"
jq -e --arg e "$erase" '.[$e].user' <<<"$(state "$ctl")" >/dev/null \
  || fail "control: without the export the erased account should come back (the drill proves nothing)"
ok "control: without the tombstone export the erased account comes back — the export keeps it erased"
if restore_into "$neg" "$work/tombstones" "$work/keys/wrong.key" >"$work/neg.log" 2>&1; then
  fail "a wrong key restored something"
fi
grep -q "cannot decrypt" "$work/neg.log" || fail "wrong key: unexpected error: $(tail -n 1 "$work/neg.log")"
[ "$(PGDATABASE="$neg" psql -X -w -tA -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'")" = 0 ] \
  || fail "wrong key: the target is not empty"
ok "wrong key: refused, nothing restored"
if restore_into "$rst" "$work/tombstones" "$work/offline-copy/backup.key" >"$work/nonempty.log" 2>&1; then
  fail "a restore into a non-empty database was not refused"
fi
grep -q "is not empty" "$work/nonempty.log" || fail "non-empty target: unexpected error: $(tail -n 1 "$work/nonempty.log")"
ok "non-empty target: refused"
echo "backup-drill: PASS (retention $days days)"
