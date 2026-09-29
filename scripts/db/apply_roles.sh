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
# They are read by psql itself (\getenv), so they never appear in a command line or in ps.
# ROLES_SQL overrides the SQL file (default: db/roles.sql next to this script's repo).
set -euo pipefail

url="${1:-${DATABASE_URL:-}}"
if [ -z "$url" ]; then
  echo "apply_roles: no database URL (argument or DATABASE_URL)" >&2
  exit 2
fi
url="${url/postgresql+psycopg:/postgresql:}"

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
  psql_q <<SQL
\\getenv role_password $var
ALTER ROLE secrag_$role LOGIN PASSWORD :'role_password';
SQL
  echo "apply_roles: secrag_$role can log in (password from $var)"
done
