#!/usr/bin/env bash
# Restart check (T11.1.5; final form + gate step in T11.2.8): does a user's data survive a
# restart of the compose stack?
#
#   scripts/restart_check.sh [--project P] [--restart-project Q] [--keep]
#
#   1. `docker compose -p P up` (db → db-roles → backend; no frontend, no LLM needed)
#   2. register a throwaway account → one chat turn ("hello", answered without the LLM)
#      → note the conversation id
#   3. restart: `down` WITHOUT -v, then `up` again as project Q (default: Q = P)
#   4. login with the same credentials → read the conversation → the user message must
#      decrypt to "hello"
#   PASS → rc 0; FAIL → rc 1 with the step and the reason; usage/safety errors → rc 2.
#
# `--restart-project` other than P reproduces the 11.1 local data loss: compose derives the
# `pgdata` volume name from the project name (the checkout folder by default), so
# `-p rag_ia` and `-p rag_app` get two different databases. Until the dev volume is fixed
# (T11.2.1) that run FAILS at the login step — the red evidence of the ADR "Diagnosis".
#
# Safety: throwaway projects only. Refuses the development projects (rag_ia, rag_app), any
# project whose resolved volumes include the development volumes, and any project whose
# `pgdata` volume already exists (so the final `down -v` can only remove what this run
# created). Fresh random JWT_SECRET / DATA_MASTER_KEY per run unless set. Removes both
# projects (containers, network, volumes, locally built images) at the end unless --keep.
# Publishes the compose ports (127.0.0.1:5432/8000): the dev stack must be down.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${RESTART_CHECK_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
API="${RESTART_CHECK_API:-http://127.0.0.1:8000}"
DEV_PROJECTS=" rag_ia rag_app "
DEV_VOLUMES=" rag_ia_pgdata rag_app_pgdata "

project="secrag-restart-check" restart_project="" keep=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) project="${2:?}"; shift ;;
    --restart-project) restart_project="${2:?}"; shift ;;
    --keep) keep=1 ;;
    -h | --help) sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
restart_project="${restart_project:-$project}"

say() { echo "[restart-check] $*"; }
refuse() { echo "[restart-check] REFUSED: $*" >&2; exit 2; }

command -v docker >/dev/null || refuse "docker not found"
command -v curl >/dev/null || refuse "curl not found"

compose() { local p="$1"; shift; docker compose -p "$p" -f "$COMPOSE_FILE" "$@"; }

export JWT_SECRET="${JWT_SECRET:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')}"
export DATA_MASTER_KEY="${DATA_MASTER_KEY:-$(python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')}"
export ENV="${ENV:-prod}"

guard() {
  local p="$1" cfg vols v
  [[ "$p" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || refuse "invalid project name '$p'"
  case "$DEV_PROJECTS" in *" $p "*) refuse "'$p' is a development project — never used here" ;; esac
  cfg="$(compose "$p" config --format json)" || refuse "docker compose config failed for '$p'"
  vols="$(python3 -c \
    'import json,sys; print(" ".join(v.get("name","") for v in json.load(sys.stdin).get("volumes",{}).values()))' \
    <<<"$cfg")" || refuse "cannot read the volumes of '$p'"
  for v in $vols; do
    case "$DEV_VOLUMES" in *" $v "*) refuse "project '$p' resolves to the development volume $v" ;; esac
  done
  if docker volume inspect "${p}_pgdata" >/dev/null 2>&1; then
    refuse "volume ${p}_pgdata already exists — not a fresh throwaway project"
  fi
}
guard "$project"
[ "$restart_project" = "$project" ] || guard "$restart_project"

teardown() {
  local rc=$?
  if [ "$keep" = 1 ]; then
    say "--keep: left projects $project/$restart_project running"
  else
    say "teardown: down -v --rmi local (projects $project, $restart_project)"
    compose "$project" down -v --rmi local --remove-orphans >/dev/null 2>&1 || true
    [ "$restart_project" = "$project" ] \
      || compose "$restart_project" down -v --rmi local --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap teardown EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  say "step: $1"
  echo "RESTART CHECK: FAIL — $2"
  exit 1
}

up() {
  say "compose -p $1 up (db, db-roles, backend)"
  compose "$1" up -d --build --quiet-pull backend >/dev/null 2>&1 \
    || fail "start $1" "docker compose up failed for project $1"
  local _
  for _ in $(seq 1 120); do
    curl -fsS "$API/health" >/dev/null 2>&1 && return 0
    sleep 2
  done
  fail "start $1" "backend of project $1 not healthy after 240 s"
}

# http <method> <path> <token|-> [json body] → "<status>\n<body>"
http() {
  local extra=()
  [ "$3" = - ] || extra+=(-H "Authorization: Bearer $3")
  [ -z "${4:-}" ] || extra+=(--data "$4")
  curl -sS -o - -w '\n%{http_code}' -X "$1" "${extra[@]}" \
    -H 'Content-Type: application/json' "$API$2"
}
status_of() { tail -n1 <<<"$1"; }
body_of() { sed '$d' <<<"$1"; }
json_get() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

email="restart-check-$(python3 -c 'import secrets; print(secrets.token_hex(6))')@example.com"
password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
creds="{\"email\": \"$email\", \"password\": \"$password\"}"

up "$project"

res="$(http POST /auth/register - "$creds")"
[ "$(status_of "$res")" = 201 ] || fail "register" "HTTP $(status_of "$res") on /auth/register"
token="$(body_of "$res" | json_get access_token)"
say "registered a throwaway account"

stream="$(curl -sS -N -X POST -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
            --data '{"question": "hello"}' "$API/chat/stream")"
conv="$(python3 -c '
import json, sys
for line in sys.stdin.read().splitlines():
    if line.startswith("data:"):
        event = json.loads(line[5:])
        if event.get("type") == "done":
            print(event["conversation_id"])
' <<<"$stream")"
[ -n "$conv" ] || fail "conversation" "no 'done' event with a conversation id from /chat/stream"
say "conversation created (one encrypted user message)"

vol_before="${project}_pgdata"
say "restart: compose -p $project down (volumes kept)"
compose "$project" down >/dev/null 2>&1
up "$restart_project"
vol_after="${restart_project}_pgdata"

res="$(http POST /auth/login - "$creds")"
if [ "$(status_of "$res")" != 200 ]; then
  fail "login after restart" "HTTP $(status_of "$res") on /auth/login — the account is gone: the \
restarted stack runs on volume $vol_after, the data was written to $vol_before \
(compose names the volume after the project)"
fi
token="$(body_of "$res" | json_get access_token)"

res="$(http GET "/conversations/$conv" "$token")"
[ "$(status_of "$res")" = 200 ] || fail "read after restart" "HTTP $(status_of "$res") on /conversations/<id>"
first="$(body_of "$res" | python3 -c 'import json,sys; print(json.load(sys.stdin)["messages"][0]["content"])')"
[ "$first" = hello ] || fail "decrypt after restart" "the stored message did not decrypt to the original text"

say "login + decrypted read OK after the restart ($vol_before → $vol_after)"
echo "RESTART CHECK: PASS"
