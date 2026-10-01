#!/usr/bin/env bash
# Restore an encrypted backup (T11.2.10) — run by the OWNER on their own machine: only they
# hold the age private key (R4-1: password manager + an offline copy).
#
#   RESTORE_DATABASE_URL=postgresql://<owner>@<host>:<port>/<empty database> \
#     scripts/db/restore.sh --dump secrag-<ts>.dump.age --identity <private key file> \
#                           --tombstones-dir <exported tombstones>
#
# Steps (the app stays closed until the last one):
#   1. checks: the dump is an age file; the identity is a private file (0600/0400, yours, no
#      symlink, not inside a git work tree); the tombstone exports are valid; the target
#      database is EMPTY (no relation in `public`) and the roles of db/roles.sql exist
#   2. decrypt with the identity (streamed — no plaintext dump on disk) → pg_restore as the
#      owner (--no-owner), one transaction, stop at the first error, from a FILTERED list
#      (rag_app.restore_toc; one summary line says what was skipped): the extensions the cloud
#      provider manages (MANAGED_EXTENSIONS below + RESTORE_SKIP_EXTENSIONS, space-separated)
#      and their comments; ACLs in pg_catalog and on schema public; default-privilege entries
#      (step 3 sets them for this owner); ACL entries naming a role the target lacks (their
#      grants to existing roles are re-applied). Grants on the app's tables are kept. Any
#      other extension in the dump must be available on the target (checked first)
#   3. db/roles.sql (default privileges), `alembic upgrade head`
#   4. the UNION of the restored tombstones and every exported one (Blob `tombstones/` —
#      pulled by backup-pull.sh — or the local export folder), then `replay_deletions`
#      (every tombstoned account the dump brought back loses its key again, is scrubbed and
#      re-queued) and one purger run as the owner (removes their rows; no export here — the
#      running purge Job exports) with --until-done: if it stops at its time limit
#      (RESTORE_PURGE_MAX_SECONDS, default the purger's 1500 s) or fails with a tombstone
#      still open, the restore FAILS — no "DONE" (DA-G1-8); every erased account stays
#      erased (R4-3, R5-2)
#
# --tombstones-dir is mandatory (an empty directory is valid only if nothing was ever erased).
# The target URL comes from the environment, never argv (it carries the password).
# The app on the restored database needs the SAME DATA_MASTER_KEY the source used (the dump
# holds the wrapped user keys and the master-key fingerprint; any other key fails closed).
set -euo pipefail

# shellcheck source=scripts/db/backup_lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup_lib.sh"

usage() { sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; }

# Extensions the cloud provider installs and manages on its servers, which no other server
# has: never restored (with their COMMENT). Add a name here when a provider adds one; for a
# one-off restore, RESTORE_SKIP_EXTENSIONS="name …" adds to it.
#   azure      Azure Database for PostgreSQL Flexible Server (CREATE EXTENSION azure)
#   pgaadauth  Azure: Microsoft Entra ID authentication
MANAGED_EXTENSIONS=(azure pgaadauth)

dump="" identity="" tombstones=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dump) shift; dump="${1:-}" ;;
    --identity) shift; identity="${1:-}" ;;
    --tombstones-dir) shift; tombstones="${1:-}" ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
[ -n "$dump" ] && [ -n "$identity" ] && [ -n "$tombstones" ] || { usage >&2; exit 2; }
[ -n "${RESTORE_DATABASE_URL:-}" ] || die "RESTORE_DATABASE_URL (owner, empty target database) is not set"
purge_limit=()
if [ -n "${RESTORE_PURGE_MAX_SECONDS:-}" ]; then
  [[ "$RESTORE_PURGE_MAX_SECONDS" =~ ^[0-9]+$ ]] || die "RESTORE_PURGE_MAX_SECONDS must be a whole number of seconds"
  purge_limit=(--max-seconds "$RESTORE_PURGE_MAX_SECONDS")
fi
for tool in age pg_restore psql; do
  command -v "$tool" >/dev/null || die "$tool not found"
done
py="$(secrag_python)"

# --- 1. checks ----------------------------------------------------------------------------
[ -f "$dump" ] || die "no dump file $dump"
is_age_file "$dump" || die "$dump is not an age-encrypted file"
[ -f "$identity" ] && [ ! -L "$identity" ] || die "the identity must be a regular file (no symlink)"
[ "$(stat -c %u "$identity")" = "$(id -u)" ] || die "the identity file is not owned by you"
case "$(stat -c %a "$identity")" in
  600 | 400) ;;
  *) die "the identity file must be mode 600 or 400 (it is the PRIVATE key)" ;;
esac
if inside_git_work_tree "$identity"; then
  die "refusing an identity inside a git work tree — the private key never goes near a repository"
fi
"$py" -m rag_app.tombstones validate --dir "$tombstones"

pg_env_from_url "$RESTORE_DATABASE_URL"
psql_t() { psql -X -w -q -tA -v ON_ERROR_STOP=1 "$@"; }
relations="$(psql_t -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'")" \
  || die "cannot connect to the target database"
[ "$relations" = 0 ] \
  || die "the target database $PGDATABASE is not empty ($relations relations in public) — restore only into a new, empty database"
roles="$(psql_t -c "SELECT count(*) FROM pg_roles WHERE rolname IN ('secrag_purger', 'secrag_backup')")"
[ "$roles" = 2 ] || die "the roles of db/roles.sql do not exist on the target server — run scripts/db/apply_roles.sh first"
echo "restore: target $PGDATABASE@$PGHOST:$PGPORT as $PGUSER is empty; roles present"

# --- 2. decrypt + pg_restore --------------------------------------------------------------
work="$(mktemp -d "${TMPDIR:-/tmp}/secrag-restore.XXXXXX")"
chmod 700 "$work"
trap 'rm -rf -- "$work"' EXIT
# age_decrypt_to, not a bare pipeline: `pg_restore -l` stops reading after the TOC, so age dies
# of SIGPIPE on any real-size dump — not a wrong key (F-2026-10-01-R).
rc=0
age_decrypt_to "$identity" "$dump" pg_restore -l >"$work/toc" || rc=$?
case "$rc" in
  0) ;;
  10) die "cannot decrypt $dump with this identity (age rc $AGE_DECRYPT_RC: wrong key, or a corrupt/truncated file) — nothing restored" ;;
  *) die "$dump decrypts, but pg_restore cannot read it as a pg_dump archive (pg_restore -l rc $AGE_READER_RC) — nothing restored" ;;
esac
grep -qv '^;' "$work/toc" || die "$dump decrypts, but its archive lists no entries — nothing restored"
echo "restore: decrypted; $(grep -vc '^;' "$work/toc") archive entries"
# The filtered list (rag_app.restore_toc): render the ACL entries (no data) to read the roles
# they name, then keep what this server can take.
skip_ext=()
# shellcheck disable=SC2206  # RESTORE_SKIP_EXTENSIONS is a space-separated list of names
for ext in "${MANAGED_EXTENSIONS[@]}" ${RESTORE_SKIP_EXTENSIONS:-}; do skip_ext+=(--skip-extension "$ext"); done
psql_t -c "SELECT rolname FROM pg_roles" >"$work/roles"
psql_t -c "SELECT name FROM pg_available_extensions" >"$work/extensions"
"$py" -m rag_app.restore_toc acl-list --toc "$work/toc" "${skip_ext[@]}" >"$work/acl.list"
: >"$work/acl.sql"
if [ -s "$work/acl.list" ]; then
  rc=0
  age_decrypt_to "$identity" "$dump" pg_restore -v -f "$work/acl.sql" -L "$work/acl.list" 2>"$work/acl.log" || rc=$?
  [ "$rc" = 0 ] || die "cannot read the ACL entries of $dump (age rc $AGE_DECRYPT_RC, pg_restore rc $AGE_READER_RC): $(grep -v '^pg_restore: \(creating\|processing\|connecting\)' "$work/acl.log" | tail -n 2) — nothing restored"
fi
"$py" -m rag_app.restore_toc filter --toc "$work/toc" --acl-sql "$work/acl.sql" \
  --roles "$work/roles" --extensions "$work/extensions" "${skip_ext[@]}" \
  --out "$work/toc.restore" --extra-sql "$work/acl.extra.sql" \
  || die "the dump cannot be restored on this server (above) — nothing restored"
rc=0
age_decrypt_to "$identity" "$dump" \
  pg_restore -w --no-owner --single-transaction --exit-on-error -L "$work/toc.restore" -d "$PGDATABASE" \
  || rc=$?
case "$rc" in
  0) ;;
  10) die "decryption failed during the restore (age rc $AGE_DECRYPT_RC; pg_restore rc $AGE_READER_RC) — do not reopen; drop $PGDATABASE and restore again into a new, empty database" ;;
  *) die "pg_restore failed (rc $AGE_READER_RC) — its single transaction was rolled back, nothing restored" ;;
esac
echo "restore: pg_restore done (one transaction)"
if [ -s "$work/acl.extra.sql" ]; then
  psql -X -w -q -v ON_ERROR_STOP=1 --single-transaction -f "$work/acl.extra.sql" \
    || die "re-applying the grants of ACL entries that named missing roles failed — do not reopen; drop $PGDATABASE and restore again into a new, empty database"
  echo "restore: grants to existing roles from those ACL entries re-applied"
fi

# --- 3. roles + migrations ----------------------------------------------------------------
roles_sql="$SECRAG_REPO_ROOT/db/roles.sql"
[ -f "$roles_sql" ] || die "db/roles.sql not found at $roles_sql"
psql -X -w -q -v ON_ERROR_STOP=1 -f "$roles_sql"
echo "restore: db/roles.sql applied"
(
  cd "$SECRAG_REPO_ROOT/backend"
  DATABASE_URL="$RESTORE_DATABASE_URL" "$py" -m alembic upgrade head 2>&1 | sed 's/^/  /'
  exit "${PIPESTATUS[0]}"
)

# --- 4. tombstones: union + replay + purge -------------------------------------------------
DATABASE_URL="$RESTORE_DATABASE_URL" "$py" -m rag_app.tombstones restore-union --dir "$tombstones"
DATABASE_URL="$RESTORE_DATABASE_URL" "$py" -m rag_app.erasure purge --no-export --until-done "${purge_limit[@]}" \
  || die "the purger run after the replay did not finish every erasure — do not reopen; run it again until it exits 0 (python -m rag_app.erasure purge --no-export --until-done)"
echo "restore: DONE — every tombstoned account is erased again; the app may reopen"
echo "restore: start the app with the SAME DATA_MASTER_KEY as the source database (escrowed with the age key)"
