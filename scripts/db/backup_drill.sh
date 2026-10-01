#!/usr/bin/env bash
# Backup drill (T11.2.10; gate step `backup-drill`): backup → erase a test user → restore →
# the user stays erased; decrypt with the OFFLINE copy of the key; files older than the retention (X9) removed.
#
#   DRILL_ADMIN_URL=postgresql://<superuser>:<pw>@127.0.0.1:<port>/postgres \
#   SECRAG_BACKUP_PASSWORD=<secrag_backup password> SECRAG_PURGER_PASSWORD=<secrag_purger password> \
#   scripts/db/backup_drill.sh
#
# Runs ONLY on a local throwaway server (the gate project): the admin URL must be loopback and
# not port 5432 (the development database). Everything it creates is thrown away: databases
# secrag_drill_*, a mktemp work directory, and THROWAWAY age keypairs (never the real backup
# key; key files are shredded at the end and nothing prints a private key).
#
#   1. fresh database, db/roles.sql, alembic upgrade head, two synthetic accounts
#      (keep / erase) with keys, a conversation and messages, and a 2 MiB table of random
#      bytes (drill_bulk) so the dump is far larger than a pipe buffer, like a real one
#      (F-2026-10-01-R: a tiny dump hid that `age -d | pg_restore -l` dies of SIGPIPE)
#   2. throwaway keypair; its private key is copied to an "offline" folder (the password
#      manager / offline copy of R4-1) and the working copy is shredded
#   3. backup.sh --file as secrag_backup into a folder that already holds a 15-day-old dump,
#      a 13-day-old dump and an unrelated file → only the 15-day-old one is removed; the
#      dump is > 1 MiB
#   4. erase the test account with the API's request path (key gone, tombstone pending), then
#      one purger run AS secrag_purger (11.2b): rows removed, tombstone done, export written
#   5. restore.sh with the OFFLINE key into a new empty database (union → replay_deletions →
#      purger) → the erased account stays erased (tombstone "done", no key/conversation/
#      message), the kept one and drill_bulk are intact
#   6. controls: without the tombstone export the erased account comes back (the export is
#      what keeps it erased); a purger pass that stops at its time limit with an erasure
#      still open fails the restore — no "DONE" (DA-G1-8); a wrong key restores nothing; an
#      age file that is not a pg_dump archive is refused as such (not as a wrong key); a
#      non-empty target is refused
set -euo pipefail

# shellcheck source=scripts/db/backup_lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup_lib.sh"

[ -n "${DRILL_ADMIN_URL:-}" ] || die "DRILL_ADMIN_URL is not set"
[ -n "${SECRAG_BACKUP_PASSWORD:-}" ] || die "SECRAG_BACKUP_PASSWORD is not set"
[ -n "${SECRAG_PURGER_PASSWORD:-}" ] || die "SECRAG_PURGER_PASSWORD is not set"
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
ctl="secrag_drill_ctl_$suffix" neg="secrag_drill_neg_$suffix" lim="secrag_drill_lim_$suffix"
work="$(mktemp -d "${TMPDIR:-/tmp}/secrag-backup-drill.XXXXXX")"
chmod 700 "$work"
cleanup() {
  local db
  for db in "$src" "$rst" "$ctl" "$neg" "$lim"; do
    admin_psql -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" >/dev/null 2>&1 || true
  done
  find "$work" -type f -name '*.key' -exec shred -u {} + 2>/dev/null || true
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "  ok: $*"; }
fail() { echo "  FAIL: $*"; exit 1; }

# --- 1. source database with two accounts -------------------------------------------------
for db in "$src" "$rst" "$ctl" "$neg" "$lim"; do admin_psql -c "CREATE DATABASE $db"; done
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
# F-2026-10-01-R: 2048 rows × 1 KiB of random bytes (incompressible) → a dump of ~2 MiB, far
# above the 64 KiB pipe buffer, so every `age -d | <reader>` in restore.sh meets a reader that
# stops early (the 1.8 MB Azure dump did; the tiny drill dump never did).
PGDATABASE="$src" psql -X -w -q -v ON_ERROR_STOP=1 -c "
  CREATE TABLE drill_bulk (id int PRIMARY KEY, blob bytea NOT NULL);
  INSERT INTO drill_bulk
    SELECT g, decode((SELECT string_agg(md5(random()::text || g || i), '') FROM generate_series(1, 64) i), 'hex')
    FROM generate_series(1, 2048) g;"
bulk_digest() { PGDATABASE="$1" psql -X -w -tA -c "SELECT count(*) || ':' || md5(string_agg(blob, ''::bytea ORDER BY id)) FROM drill_bulk"; }
bulk="$(bulk_digest "$src")"
ok "source database $src: drill_bulk (2 MiB of random bytes) $bulk"

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
size="$(stat -c %s "$dump")"
[ "$size" -gt 1048576 ] || fail "the dump is only $size bytes — the drill must use one above the pipe buffer (F-2026-10-01-R)"
if age -d -i "$work/keys/wrong.key" "$dump" >/dev/null 2>&1; then fail "a wrong key decrypts the dump"; fi
ok "backup as secrag_backup: $(basename "$dump") (age, $size bytes); > $days days removed, $((days - 1)) days kept, other files untouched"

state() { DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$1")" "$py" -m rag_app.devtools.backup_drill state --user "$keep" --user "$erase"; }

# --- 4. erase the test user (request path) + one purger run as secrag_purger ---------------
DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$src")" "$py" -m rag_app.devtools.backup_drill erase --user "$erase" >/dev/null
jq -e --arg e "$erase" '.[$e] | .user and (.key | not) and .conversations == 1 and .tombstone == "pending"' <<<"$(state "$src")" >/dev/null   || fail "request path: expected key gone, rows still there, tombstone pending: $(jq -c --arg e "$erase" '.[$e]' <<<"$(state "$src")")"
DATABASE_URL="$(url_for secrag_purger "$SECRAG_PURGER_PASSWORD" "$src")"   "$py" -m rag_app.erasure purge --export-dir "$work/tombstones" | sed 's/^/  /'
[ "${PIPESTATUS[0]}" = 0 ] || fail "the purger run failed"
jq -e --arg e "$erase" '.[$e] | (.user | not) and (.key | not) and .conversations == 0 and .messages == 0 and .tombstone == "done"' <<<"$(state "$src")" >/dev/null   || fail "purger: the erased account was not purged: $(jq -c --arg e "$erase" '.[$e]' <<<"$(state "$src")")"
ls "$work/tombstones"/tombstones-*.jsonl >/dev/null 2>&1 || fail "the purger wrote no tombstone export"
ok "erased the test account after the backup (request path → purger as secrag_purger: done); tombstones exported"
restore_into() { # restore_into <db> <tombstone dir> <identity> [dump]
  RESTORE_DATABASE_URL="$(url_for "$admin_user" "$admin_pw" "$1")" \
    bash "$SECRAG_DB_DIR/restore.sh" --dump "${4:-$dump}" --identity "$3" --tombstones-dir "$2"
}

# --- 5. restore with the offline key: the user stays erased -------------------------------
restore_into "$rst" "$work/tombstones" "$work/offline-copy/backup.key" 2>&1 | sed 's/^/  /'
s="$(state "$rst")"
jq -e --arg k "$keep" '.[$k] | .user and .key and .conversations == 1 and .messages == 2 and .tombstone == null' <<<"$s" >/dev/null \
  || fail "the kept account is not intact after the restore: $(jq -c --arg k "$keep" '.[$k]' <<<"$s")"
jq -e --arg e "$erase" '.[$e] | (.user | not) and (.key | not) and .conversations == 0 and .messages == 0 and .tombstone == "done"' <<<"$s" >/dev/null \
  || fail "the erased account came back after the restore: $(jq -c --arg e "$erase" '.[$e]' <<<"$s")"
[ "$(bulk_digest "$rst")" = "$bulk" ] || fail "drill_bulk differs after the restore: $(bulk_digest "$rst") (source $bulk)"
ok "restore (offline key) into $rst: erased account stays erased (tombstone done, no key/conversation/message); kept account and drill_bulk intact"

# --- 6. controls --------------------------------------------------------------------------
restore_into "$ctl" "$work/empty" "$work/offline-copy/backup.key" >"$work/ctl.log" 2>&1 \
  || fail "control restore failed: $(tail -n 1 "$work/ctl.log")"
jq -e --arg e "$erase" '.[$e].user' <<<"$(state "$ctl")" >/dev/null \
  || fail "control: without the export the erased account should come back (the drill proves nothing)"
ok "control: without the tombstone export the erased account comes back — the export keeps it erased"
# DA-G1-8: a purger pass that hits its time limit (0 s here) with the erasure still open.
if RESTORE_PURGE_MAX_SECONDS=0 restore_into "$lim" "$work/tombstones" "$work/offline-copy/backup.key" >"$work/lim.log" 2>&1; then
  fail "a restore whose purger pass stopped at its time limit exited 0"
fi
if grep -q "restore: DONE" "$work/lim.log"; then fail "time limit: the restore printed DONE with an erasure still open"; fi
grep -q "still open after this run" "$work/lim.log" || fail "time limit: unexpected error: $(tail -n 1 "$work/lim.log")"
ok "time limit: the purger pass stopped with an erasure still open → restore failed, no DONE"
if restore_into "$neg" "$work/tombstones" "$work/keys/wrong.key" >"$work/neg.log" 2>&1; then
  fail "a wrong key restored something"
fi
grep -q "cannot decrypt" "$work/neg.log" || fail "wrong key: unexpected error: $(tail -n 1 "$work/neg.log")"
[ "$(PGDATABASE="$neg" psql -X -w -tA -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'")" = 0 ] \
  || fail "wrong key: the target is not empty"
ok "wrong key: refused, nothing restored"
# An age file (right key) that is not a pg_dump archive, larger than the pipe buffer: refused
# as such — not as a wrong key (pg_restore gives up after the header and age dies of SIGPIPE).
head -c 2097152 /dev/urandom | age -r "$recipient" >"$work/notadump.age"
if restore_into "$neg" "$work/tombstones" "$work/offline-copy/backup.key" "$work/notadump.age" >"$work/notadump.log" 2>&1; then
  fail "an age file that is not a pg_dump archive restored something"
fi
grep -q "cannot read it as a pg_dump archive" "$work/notadump.log" \
  || fail "not a pg_dump archive: unexpected error: $(tail -n 1 "$work/notadump.log")"
ok "not a pg_dump archive (2 MiB, right key): refused as such, not as a wrong key"
if restore_into "$rst" "$work/tombstones" "$work/offline-copy/backup.key" >"$work/nonempty.log" 2>&1; then
  fail "a restore into a non-empty database was not refused"
fi
grep -q "is not empty" "$work/nonempty.log" || fail "non-empty target: unexpected error: $(tail -n 1 "$work/nonempty.log")"
ok "non-empty target: refused"
echo "backup-drill: PASS (retention $days days)"
