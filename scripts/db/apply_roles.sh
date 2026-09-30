#!/usr/bin/env bash
# Apply db/roles.sql, then give LOGIN + a password to the roles whose password is set in the
# environment (T11.0.13). Runs before every migrate: compose (db-roles service), CI, the gate.
#
#   scripts/db/apply_roles.sh [<database url>]      default: $DATABASE_URL
#
# The URL may use SQLAlchemy's "+psycopg" driver suffix (stripped for psql). Connect as the
# database owner — the role that runs Alembic (see db/roles.sql).
#
# Passwords (optional; a role without one stays NOLOGIN):
#   SECRAG_PURGER_PASSWORD   → secrag_purger
#   SECRAG_BACKUP_PASSWORD   → secrag_backup
# The plaintext never leaves this machine (DA-C-3): scram_verifier.pl computes a
# SCRAM-SHA-256 verifier client-side and only that is sent (`PASSWORD 'SCRAM-SHA-256$…'`),
# so server logs (log_statement=ddl/all, or the failing statement at log_min_error_statement)
# can never contain the password. Password and verifier travel through the environment
# (psql's \getenv), never through a command line (ps).
# ROLES_SQL overrides the SQL file (default: db/roles.sql next to this script's repo).
set -euo pipefail

url="${1:-${DATABASE_URL:-}}"
if [ -z "$url" ]; then
  echo "apply_roles: no database URL (argument or DATABASE_URL)" >&2
  exit 2
fi
url="${url/postgresql+psycopg:/postgresql:}"
# DA-F-5: a password inside the URL moves to PGPASSWORD (this process' environment), so it
# never reaches psql's command line (ps). Percent-escapes are decoded as libpq would.
if [[ "$url" =~ ^(postgresql://[^:/@]+):([^@/]*)@(.*)$ ]]; then
  url_pw="${BASH_REMATCH[2]}"
  PGPASSWORD="$(printf '%b' "${url_pw//%/\\x}")"
  export PGPASSWORD
  url="${BASH_REMATCH[1]}@${BASH_REMATCH[3]}"
  unset url_pw
fi

here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
roles_sql="${ROLES_SQL:-$here/../../db/roles.sql}"
[ -f "$roles_sql" ] || { echo "apply_roles: $roles_sql not found" >&2; exit 2; }

psql_q() { psql "$url" -X -q -v ON_ERROR_STOP=1 "$@"; }

psql_q -f "$roles_sql"
echo "apply_roles: $(basename "$roles_sql") applied"

for role in purger backup; do
  var="SECRAG_${role^^}_PASSWORD"
  if [ -z "${!var:-}" ]; then
    echo "apply_roles: secrag_$role stays NOLOGIN ($var not set)"
    continue
  fi
  SECRAG_ROLE_VERIFIER="$(perl "$here/scram_verifier.pl" "$var")"
  if ! [[ "$SECRAG_ROLE_VERIFIER" =~ ^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$ ]]; then
    echo "apply_roles: no valid SCRAM verifier for secrag_$role — nothing sent" >&2
    exit 1
  fi
  export SECRAG_ROLE_VERIFIER
  psql_q <<SQL
\\getenv role_verifier SECRAG_ROLE_VERIFIER
ALTER ROLE secrag_$role LOGIN PASSWORD :'role_verifier';
SQL
  unset SECRAG_ROLE_VERIFIER
  echo "apply_roles: secrag_$role can log in (password from $var, sent as a SCRAM verifier)"
done
