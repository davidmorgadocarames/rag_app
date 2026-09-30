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
#      owner, one transaction, stop at the first error (default-privilege entries of the
#      source owner are skipped; step 3 sets them for this owner)
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

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }

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
if ! age -d -i "$identity" "$dump" | pg_restore -l >"$work/toc"; then
  die "cannot decrypt $dump with this identity (wrong key?) — nothing restored"
fi
grep -v ' DEFAULT ACL ' "$work/toc" >"$work/toc.restore" || true
echo "restore: decrypted; $(grep -vc '^;' "$work/toc") archive entries ($(grep -c ' DEFAULT ACL ' "$work/toc" || true) default-privilege entries left to db/roles.sql)"
age -d -i "$identity" "$dump" \
  | pg_restore -w --no-owner --single-transaction --exit-on-error -L "$work/toc.restore" -d "$PGDATABASE"
echo "restore: pg_restore done (one transaction)"

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
