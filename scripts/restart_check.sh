#!/usr/bin/env bash
# Restart check (T11.1.5 red → T11.2.8 final, gate step `restart-check`): does a user's data
# survive a restart of the compose stack, and does a changed master key refuse to start?
#
#   scripts/restart_check.sh [--project P] [--restart-project Q] [--restart-master-key] [--keep]
#
#   0. refuse unless everything is throwaway (see Safety); the API port must be free
#   1. `compose -p P up db`; the BACKEND IMAGE ALONE is started once without migrate: it must
#      exit and leave no `alembic_version` (it never migrates, T11.2.5)
#   2. `compose -p P up backend`: db → db-roles → migrate (one-shot, exit 0, head) → backend
#   3. register a dedicated throwaway account → one chat turn ("hello", answered without the
#      LLM) → note the conversation id
#   4. restart: `down` WITHOUT -v, then `up` as project Q (default Q = P) → login → the
#      conversation's first message must decrypt to "hello"
#   5. --restart-master-key (DA-D-2): `down`, `up` with a NEW random DATA_MASTER_KEY → the
#      backend must REFUSE to start with a master-key fingerprint mismatch (T11.2.4). Before
#      T11.2.4 it started and every read failed (500) — that is reported as FAIL. Then `up`
#      again with the original key (must be healthy).
#   6. erase the account (DELETE /account → 202, asynchronous erasure) → login must then fail (401)
#   PASS → rc 0; FAIL → rc 1 with the step and the ACTUAL reason; usage/safety errors → rc 2.
#
# Before T11.2.1, `--restart-project Q` other than P failed at step 4 (compose derived the
# volume from the project name: the red evidence of the ADR "Diagnosis"). The dev volume is
# now one fixed external volume, so the same run passes.
#
# Safety: runs the REAL docker-compose.yml plus scripts/restart_check.compose.yml, which swaps
# the external dev volume for a throwaway one (created here, removed at the end) and publishes
# only the API on 127.0.0.1:$RESTART_CHECK_API_PORT (default 18000; no DB port), so it can run
# while the development stack is up. Refuses the development projects (rag_ia, rag_app), any
# resolved development volume, an existing throwaway volume and a busy API port. Fresh
# random JWT_SECRET / DATA_MASTER_KEY per run unless set. Removes both projects (containers,
# network, locally built images) and the throwaway volume at the end unless --keep.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${RESTART_CHECK_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
OVERRIDE_FILE="$ROOT/scripts/restart_check.compose.yml"
export RESTART_CHECK_API_PORT="${RESTART_CHECK_API_PORT:-18000}"
API="http://127.0.0.1:$RESTART_CHECK_API_PORT"
DEV_PROJECTS=" rag_ia rag_app "
DEV_VOLUMES=" rag_ia_pgdata rag_app_pgdata "
UP_TIMEOUT="${RESTART_CHECK_UP_TIMEOUT:-300}"

project="secrag-rc" restart_project="" keep=0 master_key_step=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) project="${2:?}"; shift ;;
    --restart-project) restart_project="${2:?}"; shift ;;
    --restart-master-key) master_key_step=1 ;;
    --keep) keep=1 ;;
    -h | --help) sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
restart_project="${restart_project:-$project}"

say() { echo "[restart-check] $*"; }
refuse() { echo "[restart-check] REFUSED: $*" >&2; exit 2; }

for p in "$project" "$restart_project"; do
  [[ "$p" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || refuse "invalid project name '$p'"
  case "$DEV_PROJECTS" in *" $p "*) refuse "'$p' is a development project — never used here" ;; esac
done
[[ "${RESTART_CHECK_API_PORT:-}" =~ ^[0-9]+$ ]] || refuse "RESTART_CHECK_API_PORT must be a number"
case "$RESTART_CHECK_API_PORT" in
  5432 | 8000 | 3000 | 11434) refuse "port $RESTART_CHECK_API_PORT belongs to the development stack" ;;
esac
command -v docker >/dev/null || refuse "docker not found"
command -v curl >/dev/null || refuse "curl not found"
docker info >/dev/null 2>&1 || refuse "docker is not reachable (start Docker Desktop)"

compose() { local p="$1"; shift; docker compose -p "$p" -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" "$@"; }

rand() { python3 -c 'import secrets,sys; print(secrets.token_hex(int(sys.argv[1])))' "$1"; }
new_master_key() { python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'; }

RESTART_CHECK_VOLUME="${project}-pgdata-$(rand 4)"
export RESTART_CHECK_VOLUME
export JWT_SECRET="${JWT_SECRET:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')}"
export DATA_MASTER_KEY="${DATA_MASTER_KEY:-$(new_master_key)}"
export ENV="${ENV:-prod}"
original_key="$DATA_MASTER_KEY"

# resolved_volumes <project> [key]: the volume names compose resolves for that project (all,
# or only the volume declared as <key>, e.g. pgdata).
resolved_volumes() {
  local cfg
  cfg="$(compose "$1" config --format json)" || return 1
  python3 -c '
import json, sys
vols = json.load(sys.stdin).get("volumes", {})
keys = sys.argv[1:] or list(vols)
print(" ".join(vols.get(k, {}).get("name", "") for k in keys))' ${2:+"$2"} <<<"$cfg"
}

guard() {
  local p="$1" vols v
  [[ "$p" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || refuse "invalid project name '$p'"
  case "$DEV_PROJECTS" in *" $p "*) refuse "'$p' is a development project — never used here" ;; esac
  vols="$(resolved_volumes "$p")" || refuse "docker compose config failed for '$p'"
  for v in $vols; do
    case "$DEV_VOLUMES" in *" $v "*) refuse "project '$p' resolves to the development volume $v" ;; esac
  done
  case " $vols " in
    *" $RESTART_CHECK_VOLUME "*) ;;
    *) refuse "project '$p' does not use the throwaway volume $RESTART_CHECK_VOLUME" ;;
  esac
}

# DA-D-7: nothing may already answer on the API port — a native uvicorn on WSL's
# 127.0.0.1 would otherwise receive the throwaway account (and write it to ITS database).
port_busy() {
  ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "(^|[:.])$RESTART_CHECK_API_PORT\$" && return 0
  curl -s -m 3 -o /dev/null "$API/health" 2>/dev/null
}

guard "$project"
[ "$restart_project" = "$project" ] || guard "$restart_project"
if docker volume inspect "$RESTART_CHECK_VOLUME" >/dev/null 2>&1; then
  refuse "volume $RESTART_CHECK_VOLUME already exists — not a fresh throwaway volume"
fi
if port_busy; then
  refuse "something already listens/answers on 127.0.0.1:$RESTART_CHECK_API_PORT — stop it (or set RESTART_CHECK_API_PORT)"
fi

volume_created=0
teardown() {
  local rc=$?
  trap '' INT TERM
  if [ "$keep" = 1 ]; then
    say "--keep: left projects $project/$restart_project and volume $RESTART_CHECK_VOLUME"
  else
    say "teardown: down -v --rmi local (projects $project, $restart_project) + volume $RESTART_CHECK_VOLUME"
    compose "$project" down -v --rmi local --remove-orphans >/dev/null 2>&1 || true
    [ "$restart_project" = "$project" ] \
      || compose "$restart_project" down -v --rmi local --remove-orphans >/dev/null 2>&1 || true
    if [ "$volume_created" = 1 ]; then
      docker volume rm "$RESTART_CHECK_VOLUME" >/dev/null 2>&1 \
        || say "WARNING: could not remove volume $RESTART_CHECK_VOLUME (docker volume rm $RESTART_CHECK_VOLUME)"
    fi
  fi
  exit "$rc"
}
trap teardown EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

fail() {
  say "step: $1"
  echo "RESTART CHECK: FAIL — $2"
  exit 1
}

docker volume create --label secrag.volume=restart-check "$RESTART_CHECK_VOLUME" >/dev/null
volume_created=1
say "throwaway volume $RESTART_CHECK_VOLUME; API on $API"

# backend_state <project>: running / exited / … of the backend container ("" if none)
backend_state() { compose "$1" ps -a --format '{{.State}}' backend 2>/dev/null | head -n1; }

# up <project>: 0 healthy; 3 the backend exited (its last logs in $UP_LOGS).
UP_LOGS=""
up() {
  local p="$1" _ state
  say "compose -p $p up (db, db-roles, migrate, backend)"
  if ! compose "$p" up -d --build --quiet-pull backend >/dev/null 2>&1; then
    UP_LOGS="$(compose "$p" logs --no-color --tail 40 migrate backend 2>&1 || true)"
    state="$(backend_state "$p")"
    [ "$state" = exited ] && return 3
    fail "start $p" "docker compose up failed for project $p (backend state: ${state:-none}); last logs: $(tail -n 5 <<<"$UP_LOGS" | tr '\n' ' ')"
  fi
  for _ in $(seq 1 "$((UP_TIMEOUT / 2))"); do
    curl -fsS -m 3 "$API/health" >/dev/null 2>&1 && return 0
    state="$(backend_state "$p")"
    if [ "$state" = exited ] || [ "$state" = dead ]; then
      UP_LOGS="$(compose "$p" logs --no-color --tail 40 backend 2>&1 || true)"
      return 3
    fi
    sleep 2
  done
  fail "start $p" "backend of project $p not healthy after ${UP_TIMEOUT} s (state: $(backend_state "$p"))"
}

db_sql() { compose "$1" exec -T db psql -U rag -d rag -X -tAc "$2"; }

# http <method> <path> <token|-> [json body] → "<body>\n<status>"
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

email="restart-check-$(rand 6)@example.com"
password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
creds="{\"email\": \"$email\", \"password\": \"$password\"}"

# --- 1. the backend image alone never migrates (T11.2.5) ---------------------------------
say "compose -p $project up db; backend image alone, without migrate"
compose "$project" up -d --wait --quiet-pull db >/dev/null 2>&1 || fail "start db" "the db service did not become healthy"
compose "$project" build --quiet backend migrate >/dev/null 2>&1 || fail "build" "docker compose build failed"
# Today it exits at once (its start-up check finds no schema); a backend that kept serving is
# stopped after 45 s. Either way the database must still be unmigrated.
rc=0
timeout -k 10 45 docker compose -p "$project" -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" \
  run --rm --no-deps -T backend >/dev/null 2>&1 || rc=$?
compose "$project" rm -fsv backend >/dev/null 2>&1 || true
av="$(db_sql "$project" "SELECT to_regclass('public.alembic_version') IS NOT NULL")"
[ "$av" = f ] || fail "backend alone" "the backend image migrated the database (alembic_version exists)"
case "$rc" in
  124 | 137) say "backend image alone: still serving after 45 s (stopped); database left unmigrated" ;;
  *) say "backend image alone: exited rc $rc; database left unmigrated (no alembic_version)" ;;
esac

# --- 2. up: migrate one-shot, then the backend ---------------------------------------------
up "$project" || fail "start $project" "the backend exited: $(tail -n 3 <<<"$UP_LOGS" | tr '\n' ' ')"
migrate_state="$(compose "$project" ps -a --format '{{.State}} {{.ExitCode}}' migrate | head -n1)"
[ "$migrate_state" = "exited 0" ] || fail "migrate" "the migrate one-shot did not exit 0 ($migrate_state)"
head="$(db_sql "$project" "SELECT version_num FROM alembic_version")"
say "migrate one-shot: exited 0, alembic $head"

# --- 3. account + one encrypted message -----------------------------------------------------
res="$(http POST /auth/register - "$creds")"
[ "$(status_of "$res")" = 201 ] || fail "register" "HTTP $(status_of "$res") on /auth/register"
token="$(body_of "$res" | json_get access_token)"
say "registered a dedicated throwaway account"

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

# login_and_read <step>: login with the account, read the conversation, decrypt check.
login_and_read() {
  local step="$1" res first
  res="$(http POST /auth/login - "$creds")"
  if [ "$(status_of "$res")" != 200 ]; then
    if [ "$(status_of "$res")" = 401 ] && [ "$vol_before" != "$vol_after" ]; then
      fail "$step" "HTTP 401 on /auth/login — the account is gone: the restarted stack runs on \
volume $vol_after, the data was written to $vol_before (a project-derived volume name)"
    fi
    fail "$step" "FAIL (unexpected): HTTP $(status_of "$res") on /auth/login (volume $vol_before → $vol_after)"
  fi
  token="$(body_of "$res" | json_get access_token)"
  res="$(http GET "/conversations/$conv" "$token")"
  [ "$(status_of "$res")" = 200 ] \
    || fail "$step" "HTTP $(status_of "$res") reading the conversation after the restart (logged in fine: \
the data is there but cannot be read — e.g. another DATA_MASTER_KEY)"
  first="$(body_of "$res" | python3 -c 'import json,sys; print(json.load(sys.stdin)["messages"][0]["content"])')"
  [ "$first" = hello ] || fail "$step" "the stored message did not decrypt to the original text"
}

# --- 4. restart (volumes kept), possibly as another project -------------------------------------
vol_before="$(resolved_volumes "$project" pgdata)"
say "restart: compose -p $project down (volumes kept)"
compose "$project" down >/dev/null 2>&1
up "$restart_project" || fail "restart $restart_project" "the backend exited: $(tail -n 3 <<<"$UP_LOGS" | tr '\n' ' ')"
vol_after="$(resolved_volumes "$restart_project" pgdata)"
login_and_read "login + read after restart"
say "login + decrypted read OK after the restart (project $project → $restart_project, volume $vol_after)"

# --- 5. a changed master key must refuse to start (DA-D-2, T11.2.4) -------------------------------
if [ "$master_key_step" = 1 ]; then
  say "restart with a NEW random DATA_MASTER_KEY (expected: refused, fingerprint mismatch)"
  compose "$restart_project" down >/dev/null 2>&1
  DATA_MASTER_KEY="$(new_master_key)"
  export DATA_MASTER_KEY
  if up "$restart_project"; then
    res="$(http POST /auth/login - "$creds")"
    code="-"
    if [ "$(status_of "$res")" = 200 ]; then
      token="$(body_of "$res" | json_get access_token)"
      code="$(status_of "$(http GET "/conversations/$conv" "$token")")"
    fi
    fail "restart with another master key" "the API STARTED with a different DATA_MASTER_KEY (login \
HTTP $(status_of "$res"), read HTTP $code): no fingerprint check — the data is silently unreadable"
  fi
  grep -q "does not match the master-key fingerprint" <<<"$UP_LOGS" \
    || fail "restart with another master key" "the backend exited, but not for a fingerprint \
mismatch: $(grep -m1 -E 'Error|error' <<<"$UP_LOGS" | cut -c1-200)"
  say "refused to start: master-key fingerprint mismatch (as expected)"
  compose "$restart_project" down >/dev/null 2>&1
  DATA_MASTER_KEY="$original_key"
  export DATA_MASTER_KEY
  up "$restart_project" || fail "restart with the original key" "the backend exited: $(tail -n 3 <<<"$UP_LOGS" | tr '\n' ' ')"
  login_and_read "login + read with the original key"
  say "original key: starts again, data readable"
fi

# --- 6. erase the dedicated account ------------------------------------------------------
res="$(http DELETE /account "$token")"
[ "$(status_of "$res")" = 202 ] || fail "erase" "HTTP $(status_of "$res") on DELETE /account"
res="$(http POST /auth/login - "$creds")"
[ "$(status_of "$res")" = 401 ] || fail "erase" "login still works after DELETE /account (HTTP $(status_of "$res"))"
say "dedicated account erased (login refused afterwards)"

echo "RESTART CHECK: PASS"
