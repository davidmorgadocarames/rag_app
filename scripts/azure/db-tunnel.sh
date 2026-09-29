#!/usr/bin/env bash
# Temporary, self-removing access to the Azure PostgreSQL Flexible Server (T11.1.2).
#
#   scripts/azure/db-tunnel.sh [options] -- psql|pg_dump [args...]
#   scripts/azure/db-tunnel.sh --sweep          # only remove stale rules, then exit
#
# 1. Sweep: every firewall rule named "secrag-tunnel-*" left by an earlier run (killed with
#    SIGKILL, machine off) is removed first — on EVERY call.
# 2. Detects this machine's public IPv4 from two independent services; both must answer
#    and agree, and the address must be public — otherwise it refuses (fail closed).
# 3. Creates ONE rule "secrag-tunnel-<UTC time>-<pid>" for exactly that address (/32).
# 4. Runs the given psql/pg_dump with sslmode=require, connecting through libpq variables
#    (never a URL or a password on the command line). Read-only by default
#    (default_transaction_read_only=on); --read-write lifts it.
# 5. ALWAYS removes the rule — success, error, Ctrl-C (SIGINT), SIGTERM, SIGHUP — and then
#    checks it is gone; if it cannot confirm that, it prints the manual command and fails.
#
# Options:
#   --password-from-app [APP]  user, database and password from the Container App's
#                              `database-url` secret (default app: secrag-backend). The
#                              secret is read into memory and handed to libpq through a
#                              temporary 0600 PGPASSFILE; it is never printed or logged.
#   --user NAME / --db NAME    otherwise: login (default: the server's administratorLogin)
#                              and database (required); the password then comes from
#                              PGPASSWORD, ~/.pgpass or psql's own prompt.
#   --read-write               allow writes (default: read-only transactions)
#   --sweep                    remove stale "secrag-tunnel-*" rules and exit
#   -g RG / -s SERVER          resource group / server (default rg-secrag / secrag-db-dmc26,
#                              or DB_TUNNEL_RG / DB_TUNNEL_SERVER)
#
# Needs the Linux Azure CLI (`az`, logged in) and the PostgreSQL 16 client. Only one tunnel
# at a time: a second call sweeps the first call's rule.
set -euo pipefail

PREFIX="secrag-tunnel-"
RG="${DB_TUNNEL_RG:-rg-secrag}"
SERVER="${DB_TUNNEL_SERVER:-secrag-db-dmc26}"
IP_SOURCES=("https://api.ipify.org" "https://checkip.amazonaws.com")

log() { echo "[db-tunnel] $*" >&2; }
die() { echo "[db-tunnel] ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

from_app="" user="" db="" read_only=1 sweep_only=0
while [ $# -gt 0 ]; do
  case "$1" in
    --password-from-app)
      from_app="secrag-backend"
      if [ $# -gt 1 ] && [[ "$2" != -* ]]; then from_app="$2"; shift; fi ;;
    --user) user="${2:?}"; shift ;;
    --db) db="${2:?}"; shift ;;
    --read-write) read_only=0 ;;
    --sweep) sweep_only=1 ;;
    -g) RG="${2:?}"; shift ;;
    -s) SERVER="${2:?}"; shift ;;
    -h | --help) usage ;;
    --) shift; break ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
  shift
done

if [ "$sweep_only" = 0 ]; then
  [ $# -gt 0 ] || usage
  case "$(basename "$1")" in
    psql | pg_dump) ;;
    *) die "only psql or pg_dump can run through the tunnel (got '$1')" ;;
  esac
  # The password must never reach argv (ps, shell history, CI logs).
  for a in "$@"; do
    case "$a" in
      *assword=* | postgres://* | postgresql://*)
        die "no connection strings or passwords in the command; the tunnel sets the connection" ;;
    esac
  done
fi

command -v az >/dev/null || die "az (Linux Azure CLI) not found"

fw() { az postgres flexible-server firewall-rule "$@" -g "$RG" -s "$SERVER"; }

stale_rules() {
  fw list --query "[?starts_with(name, '$PREFIX')].name" -o tsv
}

sweep() {
  local names name
  names="$(stale_rules)" || die "cannot list the firewall rules of $SERVER (logged in? az login)"
  for name in $names; do
    log "sweep: removing stale rule $name"
    fw delete -n "$name" --yes -o none || die "sweep: could not remove $name"
  done
}

sweep
if [ "$sweep_only" = 1 ]; then
  log "sweep done (no rule with prefix $PREFIX left)"
  exit 0
fi

# --- public IPv4 (fail closed) --------------------------------------------------------------
is_public_ipv4() {
  local ip="$1" a b c d
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  a=${BASH_REMATCH[1]} b=${BASH_REMATCH[2]} c=${BASH_REMATCH[3]} d=${BASH_REMATCH[4]}
  for o in "$a" "$b" "$c" "$d"; do [ "$o" -le 255 ] || return 1; done
  [ "$a" -eq 0 ] || [ "$a" -eq 10 ] || [ "$a" -eq 127 ] || [ "$a" -ge 224 ] && return 1
  [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ] && return 1
  [ "$a" -eq 169 ] && [ "$b" -eq 254 ] && return 1
  [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ] && return 1
  [ "$a" -eq 192 ] && [ "$b" -eq 168 ] && return 1
  return 0
}

ip=""
for src in "${IP_SOURCES[@]}"; do
  got="$(curl -4 -fsS --max-time 10 "$src" 2>/dev/null | tr -d '[:space:]')" \
    || die "public IP unknown: $src did not answer — refusing to open the firewall"
  is_public_ipv4 "$got" || die "public IP unknown: $src returned no public IPv4 — refusing"
  if [ -n "$ip" ] && [ "$got" != "$ip" ]; then
    die "public IP unknown: the sources disagree — refusing to open the firewall"
  fi
  ip="$got"
done

# --- connection parameters -----------------------------------------------------------------
host="$(az postgres flexible-server show -g "$RG" -n "$SERVER" \
          --query fullyQualifiedDomainName -o tsv)" || die "cannot read server $SERVER"
[ -n "$host" ] || die "server $SERVER has no FQDN"

password=""
if [ -n "$from_app" ]; then
  url="$(az containerapp secret show -g "$RG" -n "$from_app" --secret-name database-url \
           --query value -o tsv 2>/dev/null)" \
    || die "cannot read the database-url secret of $from_app"
  re='^postgres(ql)?(\+[a-z0-9]+)?://([^:/@]+):([^@]*)@([^:/?]+)(:[0-9]+)?/([^?]+)'
  [[ "$url" =~ $re ]] || die "the database-url secret of $from_app is not a postgres URL with a password"
  user="${BASH_REMATCH[3]}"
  password="${BASH_REMATCH[4]}"
  url_host="${BASH_REMATCH[5]}"
  db="${BASH_REMATCH[7]}"
  unset url
  # Percent-decoding (a URL-encoded password), without echoing it; literal backslashes are
  # doubled first so %b only expands the \xHH produced from %HH.
  if [[ "$password" == *%* ]]; then
    password="${password//\\/\\\\}"
    password="$(printf '%b' "${password//%/\\x}")"
  fi
  [ "$url_host" = "$host" ] || die "the secret points at another host than $SERVER — refusing"
else
  if [ -z "$user" ]; then
    user="$(az postgres flexible-server show -g "$RG" -n "$SERVER" \
              --query administratorLogin -o tsv)" || die "cannot read the admin login"
  fi
  [ -n "$db" ] || die "--db is required without --password-from-app"
fi

# --- the rule lives only while the command runs ----------------------------------------------
rule="" passfile=""

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  local rc=$? left
  # A second Ctrl-C must not abort the removal (SIG_IGN is inherited by az too).
  trap '' INT TERM HUP
  trap - EXIT
  [ -z "$passfile" ] || rm -f "$passfile"
  if [ -n "$rule" ]; then
    fw delete -n "$rule" --yes -o none >/dev/null 2>&1 || true
    if left="$(fw list --query "[?name=='$rule'].name" -o tsv 2>/dev/null)" && [ -z "$left" ]; then
      log "firewall rule $rule removed"
    else
      echo "[db-tunnel] ERROR: could not confirm that firewall rule $rule is gone. Remove it NOW:" >&2
      echo "  az postgres flexible-server firewall-rule delete -g $RG -s $SERVER -n $rule --yes" >&2
      echo "  (or run: scripts/azure/db-tunnel.sh --sweep)" >&2
      [ "$rc" -ne 0 ] || rc=1
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

rule="$PREFIX$(date -u +%Y%m%dT%H%M%SZ)-$$"
log "creating firewall rule $rule for $ip/32 on $SERVER (removed when the command ends)"
fw create -n "$rule" --start-ip-address "$ip" --end-ip-address "$ip" -o none >/dev/null \
  || die "could not create the firewall rule"

export PGHOST="$host" PGPORT=5432 PGUSER="$user" PGDATABASE="$db"
export PGSSLMODE=require PGCONNECT_TIMEOUT=20 PGAPPNAME=secrag-db-tunnel
if [ "$read_only" = 1 ]; then
  export PGOPTIONS="-c default_transaction_read_only=on"
  log "read-only session (default_transaction_read_only=on)"
else
  log "READ-WRITE session (--read-write)"
fi
if [ -n "$password" ]; then
  passfile="$(umask 077; mktemp "${TMPDIR:-/tmp}/secrag-pgpass.XXXXXX")"
  esc() { local v="${1//\\/\\\\}"; printf '%s' "${v//:/\\:}"; }
  printf '%s:%s:%s:%s:%s\n' "$(esc "$host")" 5432 "$(esc "$db")" "$(esc "$user")" \
    "$(esc "$password")" >"$passfile"
  unset password
  export PGPASSFILE="$passfile"
  unset PGPASSWORD
fi

log "running $(basename "$1") as $user on $db (sslmode=require)"
set +e
"$@"
rc=$?
set -e
exit "$rc"
