#!/usr/bin/env bash
# Phase gate: enforce the project's Definition of Done (docs/DEFINITION_OF_DONE.md).
# Non-zero exit == blocked. Modes (PHASE_PLANNING §1 rule 4, T11.0.5):
#
#   scripts/gate.sh [--fast]          fast steps only, no stack needed (default; pre-push
#                                     on phase branches)
#   scripts/gate.sh --full            fast + stack steps, in the isolated gate project
#                                     (compose.gate.yml): up → seed → steps → down -v.
#                                     A missing tool or an unreachable stack FAILS.
#   scripts/gate.sh --only a,b        just these steps (stack started only if one needs it);
#                                     a missing tool FAILS, as in --full
#   scripts/gate.sh --make-seed       (re)build the gate seed dump from data/chunks with
#                                     Ollama embeddings (corpus, embedding model or
#                                     migrations changed)
#   scripts/gate.sh --list            step names, modes and time budgets
#
# Every step runs in its own process under `timeout <budget>`; exceeding the budget fails.
# Native steps always see the GATE's DATABASE_URL / OLLAMA_HOST (exported below), never the
# development database: in --fast the DB URL points at the (stopped) gate port (GATE_DB_PORT, default 15432).
#
# Paths: REPO_ROOT is the tree being checked (a temporary worktree at the pushed SHA when
# run by .githooks/pre-push); MAIN_ROOT is the main working tree, which provides the
# git-ignored seed (.gate/), data/ and .env files. The backend venv must match REPO_ROOT's
# pins (DA-B-4): the main venv when it does, otherwise a venv cached per requirements hash
# under the git common dir (built with uv). GATE_VENV=<dir> overrides the choice.
set -uo pipefail

export PATH="$HOME/.local/bin:$PATH"
hash -r 2>/dev/null || true

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SELF")/.." && pwd)" || exit 1
cd "$REPO_ROOT" || exit 1
GIT_COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$REPO_ROOT/.git")"
MAIN_ROOT="${GATE_MAIN_ROOT:-$(dirname "$GIT_COMMON")}"
MAIN_VENV="$MAIN_ROOT/backend/.venv"
VENV="${GATE_VENV:-$MAIN_VENV}"   # final choice: resolve_venv
PY="$VENV/bin/python"
GATE_STATE="$MAIN_ROOT/.gate"
ROLES_ENV="$GATE_STATE/roles.env"   # local role passwords (git-ignored, 0600; T11.0.13)
SEED="$GATE_STATE/seed.dump"
SEED_META="$GATE_STATE/seed.meta"
CHUNKS="$MAIN_ROOT/data/chunks/chunks.jsonl"

# Venvs for trees whose pins differ from the main venv (DA-B-4).
VENV_CACHE="$GIT_COMMON/secrag-gate/venvs"
VENV_RECIPE=1                     # bump when build_venv changes
TORCH_INDEX="https://download.pytorch.org/whl/cpu"   # as in backend/Dockerfile
VENV_BUILD_BUDGET=900
VENV_CACHE_KEEP=2                 # ~1.5 GB each (torch)

COMPOSE_PROJECT="secrag-gate"
COMPOSE_FILE="$REPO_ROOT/compose.gate.yml"
# Below the Windows dynamic range: Hyper-V reserves blocks of 49152-65535 (55432 failed).
GATE_DB_PORT="${GATE_DB_PORT:-15432}"
export GATE_DB_PORT
GATE_DB_USER="secrag_gate"
GATE_DB_NAME="secrag_gate"
FULL_TARGET_SECONDS=1800   # X5: --full target in Phase 11

# name | needs the gate stack: - (no) / db (gate DB) / seed (DB + seed + Ollama) /
#        docker (own throwaway compose projects, not in --fast) | budget (s)
STEP_TABLE="
environment      - 10
secrets          - 10
git-modes        - 10
pinned-deps      - 10
venv             - 10
ruff-lint        - 60
ruff-format      - 60
mypy             - 240
pytest           - 300
gitleaks         - 180
shellcheck       - 60
schema-check     - 30
adr-links        - 30
frontend         - 600
dependency-audit - 240
db-tests         db 300
migrations-roundtrip db 180
backup-drill     db 300
restart-check    docker 900
eval             seed 1200
"
STACK_BUDGET=300   # up + migrate + seed restore

step_names() { awk 'NF==3 {print $1}' <<<"$STEP_TABLE"; }
step_field() { awk -v n="$1" -v f="$2" 'NF==3 && $1==n {print $f}' <<<"$STEP_TABLE"; }
is_step() { [ -n "$(step_field "$1" 1)" ]; }

compose() { docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" "$@"; }

# Exported to every step (each runs in its own process).
export GATE_MODE="${GATE_MODE:-fast}" REPO_ROOT MAIN_ROOT VENV PY
export PYTHONPATH="$REPO_ROOT/backend/src"

# --- helpers used inside steps ---------------------------------------------------------

# missing_tool <message>: SKIP only in --fast; FAIL in --full and --only (W22, DA-B-2:
# a step that was asked for, or a promotion gate, never passes without running).
missing_tool() {
  if [ "$GATE_MODE" != fast ]; then
    echo "  FAIL (missing tooling under --$GATE_MODE): $1"
    return 1
  fi
  echo "  SKIP (missing tooling): $1"
  return 3
}
need_venv() { [ -x "$PY" ] || { echo "  FAIL: backend venv missing at $VENV"; return 1; }; }

# Every tracked shell script: *.sh, .githooks/*, and any file with a sh/bash shebang.
tracked_scripts() {
  local path
  while IFS= read -r path; do
    case "$path" in
      *.sh | .githooks/*) echo "$path"; continue ;;
    esac
    [ -f "$path" ] || continue
    head -n 1 "$path" 2>/dev/null | grep -Eq '^#!.*[/ ](ba)?sh([[:space:]]|$)' && echo "$path"
  done < <(git ls-files)
}

# hook_status <clone root>: the pre-push gate is active for that clone (DA-B-1). Git runs
# the hook from the working-tree file and silently ignores it when it is not executable
# or when core.hooksPath does not point at .githooks.
hook_status() {
  local root="$1" hooks_path
  hooks_path="$(git -C "$root" config --get core.hooksPath 2>/dev/null)"
  if [ "$hooks_path" != .githooks ]; then
    echo "  FAIL: core.hooksPath is '${hooks_path:-unset}' in $root — the pre-push gate is NOT active (git config core.hooksPath .githooks)"
    return 1
  fi
  if [ ! -x "$root/.githooks/pre-push" ]; then
    echo "  FAIL: $root/.githooks/pre-push is not executable on disk — git ignores it silently (chmod +x .githooks/pre-push)"
    return 1
  fi
  echo "  pre-push hook active in $root (core.hooksPath=.githooks, executable on disk)"
}

# --- steps ------------------------------------------------------------------------------

step_environment() {
  grep -qi microsoft /proc/version 2>/dev/null \
    || { echo "  FAIL: not running under WSL2 (norm #1)"; return 1; }
}

step_secrets() {
  local tracked
  tracked="$(git ls-files -- .env '*/.env' '.env.*' '*/.env.*' | grep -v '\.env\.example$')"
  [ -z "$tracked" ] || { echo "  FAIL: .env files tracked by git: $tracked"; return 1; }
}

# Scripts executable in git and on disk; the pushing clone's pre-push hook active (DA-B-1).
step_git-modes() {
  local scripts=() path bad_index="" bad_disk="" rc=0
  mapfile -t scripts < <(tracked_scripts)
  echo "  ${#scripts[@]} tracked scripts"
  for path in "${scripts[@]}"; do
    [ "$(git ls-files -s -- "$path" | awk '{print $1}')" = 100755 ] || bad_index="$bad_index $path"
    [ -x "$path" ] || bad_disk="$bad_disk $path"
  done
  if [ -n "$bad_index" ]; then
    echo "  FAIL: not 100755 in git (git update-index --chmod=+x):$bad_index"
    rc=1
  fi
  if [ -n "$bad_disk" ]; then
    echo "  FAIL: not executable on disk (chmod +x; UNC writes from Windows drop it):$bad_disk"
    rc=1
  fi
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    echo "  pre-push hook activation: not applicable in CI (checked locally by gate.sh and check.sh)"
  else
    hook_status "$MAIN_ROOT" || rc=1
  fi
  return "$rc"
}

step_pinned-deps() {
  local unpinned="" line pkg
  while IFS= read -r line; do
    case "$line" in '' | '#'* | -*) continue ;; esac
    pkg="$(tr -d '[:space:]' <<<"${line%%#*}")"
    [ -z "$pkg" ] && continue
    case "$pkg" in *==*) ;; *) unpinned="$unpinned $pkg" ;; esac
  done < <(cat backend/requirements*.txt 2>/dev/null)
  [ -z "$unpinned" ] || { echo "  FAIL: unpinned dependencies:$unpinned"; return 1; }
}

# The venv has exactly this tree's pins (DA-B-4): otherwise tests, mypy and the audit
# would describe other packages than the ones being pushed.
step_venv() {
  need_venv || return 1
  echo "  $("$PY" --version 2>&1) at $VENV"
  "$PY" -m rag_app.devtools.venv_sync --root "$REPO_ROOT" 2>&1 | sed 's/^/  /'
  [ "${PIPESTATUS[0]}" -eq 0 ] || { echo "  FAIL: fix with: $VENV/bin/pip install -r backend/requirements-dev.txt"; return 1; }
}

step_ruff-lint() { need_venv && (cd backend && "$VENV/bin/ruff" check .); }
step_ruff-format() { need_venv && (cd backend && "$VENV/bin/ruff" format --check .); }
step_mypy() { need_venv && (cd backend && "$VENV/bin/mypy" src); }

step_pytest() {
  need_venv || return 1
  (cd backend && env -u TEST_DATABASE_URL -u SECRAG_REQUIRE_DB_TESTS \
    "$VENV/bin/pytest" -m "not db" -p no:cacheprovider)
}

step_gitleaks() {
  [ -x "$VENV/bin/pre-commit" ] || { missing_tool "pre-commit not installed in the venv"; return; }
  # The committed history of the checked tree (as CI scans it) and the staged changes.
  "$VENV/bin/pre-commit" run gitleaks-history --hook-stage manual --all-files \
    && "$VENV/bin/pre-commit" run gitleaks --all-files
}

step_shellcheck() {
  command -v shellcheck >/dev/null \
    || { missing_tool "shellcheck (scripts/prereqs/install.sh shellcheck)"; return; }
  local files=()
  mapfile -t files < <(tracked_scripts)
  echo "  ${#files[@]} files: ${files[*]}"
  shellcheck -x "${files[@]}"
}

step_schema-check() {
  need_venv && "$PY" -m rag_app.devtools.schema_check --root "$REPO_ROOT" --chunks "$CHUNKS"
}

# RUNBOOK_PATH (private runbook outside the repo) is optional: unset → explicit SKIP line.
step_adr-links() { need_venv && "$PY" -m rag_app.devtools.adr_links --root "$REPO_ROOT"; }

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# Toolchain floor: Node >= 22.13 (22.x) and npm >= 11 (scripts/prereqs/install.sh node).
step_frontend() {
  [ -d frontend/node_modules ] || { missing_tool "frontend/node_modules (npm ci)"; return; }
  local node_v npm_v
  node_v="$(node --version 2>/dev/null | tr -d v)"
  npm_v="$(npm --version 2>/dev/null)"
  if [ "${node_v%%.*}" != 22 ] || ! version_ge "$node_v" 22.13.0 || ! version_ge "${npm_v:-0}" 11.0.0; then
    echo "  FAIL: node ${node_v:-missing} / npm ${npm_v:-missing}; need node 22.x >= 22.13 and npm >= 11"
    return 1
  fi
  echo "  toolchain: node $node_v, npm $npm_v"
  (cd frontend && npm run lint && npm run typecheck && npm run build)
}

step_dependency-audit() {
  need_venv || return 1
  "$VENV/bin/python" -c 'import pip_audit' 2>/dev/null \
    || { missing_tool "pip-audit (pip install -r backend/requirements-dev.txt)"; return; }
  [ -d frontend/node_modules ] || { missing_tool "frontend/node_modules (npm ci)"; return; }
  "$PY" -m rag_app.devtools.dependency_audit --root "$REPO_ROOT"
}

step_db-tests() {
  need_venv || return 1
  (cd backend && SECRAG_REQUIRE_DB_TESTS=1 "$VENV/bin/pytest" -m db -p no:cacheprovider -rs)
}

# Fresh database → roles.sql twice (catalog identical) → the roles log in with the gate
# passwords → upgrade head → pg_dump as secrag_backup (default privileges, W16) →
# downgrade -1 → upgrade head. Roles are cluster-wide: stack_up already created them.
ROLES_SNAPSHOT_SQL="
SELECT 'role', rolname, rolcanlogin::text, rolsuper::text, rolinherit::text, rolcreaterole::text,
       rolcreatedb::text, rolreplication::text, rolbypassrls::text, rolconnlimit::text
  FROM pg_roles WHERE rolname LIKE 'secrag\\_%' AND rolname <> current_user
UNION ALL SELECT 'db', datname, coalesce(datacl::text, ''), '', '', '', '', '', '', ''
  FROM pg_database WHERE datname = current_database()
UNION ALL SELECT 'schema', nspname, coalesce(nspacl::text, ''), '', '', '', '', '', '', ''
  FROM pg_namespace WHERE nspname = 'public'
UNION ALL SELECT 'default', defaclobjtype::text, coalesce(defaclacl::text, ''), '', '', '', '', '', '', ''
  FROM pg_default_acl
UNION ALL SELECT 'rel', relname, coalesce(relacl::text, ''), '', '', '', '', '', '', ''
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S', 'v', 'm', 'p')
ORDER BY 1, 2, 3"

# rt_alembic <database> <alembic args…>: Alembic against one database of the gate server.
rt_alembic() {
  local db="$1"
  shift
  (cd backend && DATABASE_URL="postgresql+psycopg://$GATE_DB_USER:$GATE_DB_PASSWORD@127.0.0.1:$GATE_DB_PORT/$db" \
    "$VENV/bin/alembic" "$@" 2>&1 | sed 's/^/  /'; exit "${PIPESTATUS[0]}")
}

step_migrations-roundtrip() {
  need_venv || return 1
  if ! command -v psql >/dev/null || ! command -v pg_dump >/dev/null; then
    missing_tool "psql/pg_dump 16 (scripts/prereqs/install.sh pg)"
    return
  fi
  [ -f "$ROLES_ENV" ] || { echo "  FAIL: $ROLES_ENV missing (created by the gate stack)"; return 1; }
  set -a
  # shellcheck source=/dev/null
  . "$ROLES_ENV"
  set +a
  local db rc=0 before after role var
  db="secrag_rt_$(openssl rand -hex 4)"
  local base="postgresql://$GATE_DB_USER@127.0.0.1:$GATE_DB_PORT"
  export PGPASSWORD="$GATE_DB_PASSWORD"
  psql "$base/postgres" -X -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE $db" || return 1
  # shellcheck disable=SC2064
  trap "PGPASSWORD='$GATE_DB_PASSWORD' psql '$base/postgres' -X -q -c 'DROP DATABASE IF EXISTS $db WITH (FORCE)' >/dev/null 2>&1" EXIT
  local url="$base/$db"
  echo "  fresh database $db"
  bash "$REPO_ROOT/scripts/db/apply_roles.sh" "$url" | sed 's/^/  /' || return 1
  rt_alembic "$db" upgrade head || { echo "  FAIL: alembic upgrade head on a fresh database"; return 1; }
  rt_alembic "$db" current
  # Before any second roles.sql run: only the default privileges can make the tables the
  # migrations just created readable (W16).
  if PGPASSWORD="$SECRAG_BACKUP_PASSWORD" pg_dump \
       "postgresql://secrag_backup@127.0.0.1:$GATE_DB_PORT/$db" -Fc >/dev/null; then
    echo "  pg_dump as secrag_backup: OK (tables created after roles.sql are readable)"
  else
    echo "  FAIL: pg_dump as secrag_backup (default privileges missing?)"
    rc=1
  fi
  # Second run on the migrated database: roles, grants, default privileges and every
  # table/sequence ACL must be identical.
  before="$(psql "$url" -X -tA -F'|' -c "$ROLES_SNAPSHOT_SQL")" || return 1
  bash "$REPO_ROOT/scripts/db/apply_roles.sh" "$url" >/dev/null || return 1
  after="$(psql "$url" -X -tA -F'|' -c "$ROLES_SNAPSHOT_SQL")" || return 1
  if [ "$before" = "$after" ]; then
    echo "  roles.sql twice: catalog unchanged ($(wc -l <<<"$after") entries: roles, grants, default privileges, ACLs)"
  else
    echo "  FAIL: running roles.sql again changed the catalog:"
    diff <(echo "$before") <(echo "$after") | sed 's/^/    /'
    rc=1
  fi
  for role in purger backup; do
    var="SECRAG_${role^^}_PASSWORD"
    if PGPASSWORD="${!var}" psql "postgresql://secrag_$role@127.0.0.1:$GATE_DB_PORT/$db" \
         -X -tA -c 'SELECT current_user' >/dev/null; then
      echo "  secrag_$role logs in with the gate password"
    else
      echo "  FAIL: secrag_$role cannot log in with the gate password"
      rc=1
    fi
  done
  { rt_alembic "$db" downgrade -1 && rt_alembic "$db" upgrade head; } \
    || { echo "  FAIL: downgrade -1 / upgrade head round trip"; return 1; }
  echo "  downgrade -1 → upgrade head: OK"
  return "$rc"
}

# T11.2.10: backup (file mode, as secrag_backup) → erase a test user → restore with the
# OFFLINE copy of a throwaway age key → the user stays erased; files > retention removed;
# controls (no tombstone export → the user comes back; wrong key; non-empty target).
# Throwaway secrag_drill_* databases on the gate server only (scripts/db/backup_drill.sh).
step_backup-drill() {
  need_venv || return 1
  local tool
  for tool in age age-keygen pg_dump pg_restore psql jq; do
    command -v "$tool" >/dev/null || { missing_tool "$tool (scripts/prereqs/install.sh)"; return; }
  done
  [ -f "$ROLES_ENV" ] || { echo "  FAIL: $ROLES_ENV missing (created by the gate stack)"; return 1; }
  set -a
  # shellcheck source=/dev/null
  . "$ROLES_ENV"
  set +a
  DRILL_ADMIN_URL="postgresql://$GATE_DB_USER:$GATE_DB_PASSWORD@127.0.0.1:$GATE_DB_PORT/postgres" \
    SECRAG_PYTHON="$PY" bash "$REPO_ROOT/scripts/db/backup_drill.sh" 2>&1 | sed 's/^/  /'
  return "${PIPESTATUS[0]}"
}

# T11.2.8: data survives down/up (no -v) even across compose project names (fixed external
# volume, T11.2.1); the backend image never migrates (T11.2.5); a changed master key refuses
# to start (T11.2.4, DA-D-2); the dedicated account is erased at the end. Throwaway projects
# `secrag-gate-rc*` and a throwaway external volume, API on 127.0.0.1:18000 (no DB port) —
# never the development volume, and it can run with the development stack up.
step_restart-check() {
  docker info >/dev/null 2>&1 \
    || { echo "  FAIL: docker is not reachable from WSL — start Docker Desktop"; return 1; }
  env -u JWT_SECRET -u DATA_MASTER_KEY -u ENV bash "$REPO_ROOT/scripts/restart_check.sh" \
    --project secrag-gate-rc --restart-project secrag-gate-rc2 --restart-master-key 2>&1 \
    | sed 's/^/  /'
  return "${PIPESTATUS[0]}"
}

step_eval() {
  need_venv || return 1
  (cd backend && "$PY" -m rag_app.eval.gate --require-stack)
}

# --- backend venv matching the pins (DA-B-4) --------------------------------------------

req_hash() { # req_hash <tree>: build recipe + both requirement files
  {
    echo "recipe=$VENV_RECIPE torch-index=$TORCH_INDEX"
    cat "$1/backend/requirements.txt" "$1/backend/requirements-dev.txt" 2>/dev/null
  } | sha256sum | cut -c1-16
}

venv_in_sync() { # venv_in_sync <venv dir>: installed == REPO_ROOT's pins
  [ -x "$1/bin/python" ] \
    && "$1/bin/python" -m rag_app.devtools.venv_sync --root "$REPO_ROOT" >/dev/null 2>&1
}

# build_venv <dir>: runs in its own process under timeout (--build-venv).
build_venv() {
  local dir="$1" torch_pin="" py_version="3.12"
  command -v uv >/dev/null || { echo "FAIL: uv not found (needed to build the gate venv)"; return 1; }
  mkdir -p "$VENV_CACHE" || return 1
  exec 9>"$VENV_CACHE/.lock"
  flock 9
  [ -f "$dir/.secrag-complete" ] && return 0   # a concurrent run built it
  rm -rf "$dir"
  if [ -x "$MAIN_VENV/bin/python" ]; then
    # Same interpreter and torch build as the main venv (torch is not in the pins).
    py_version="$("$MAIN_VENV/bin/python" -c 'import platform; print(platform.python_version())')"
    torch_pin="$("$MAIN_VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("torch"))' 2>/dev/null)"
  fi
  uv venv --quiet --seed --python "$py_version" "$dir" || return 1
  uv pip install --quiet --python "$dir/bin/python" --index-url "$TORCH_INDEX" \
    "torch${torch_pin:+==$torch_pin}" || return 1
  uv pip install --quiet --python "$dir/bin/python" -r "$REPO_ROOT/backend/requirements-dev.txt" \
    || return 1
  "$dir/bin/python" -m rag_app.devtools.venv_sync --root "$REPO_ROOT" || return 1
  touch "$dir/.secrag-complete"
}

prune_venv_cache() { # keep the VENV_CACHE_KEEP most recently used cached venvs
  local old
  find "$VENV_CACHE" -mindepth 2 -maxdepth 2 -name .secrag-complete -printf '%T@ %h\n' 2>/dev/null \
    | sort -rn | tail -n +"$((VENV_CACHE_KEEP + 1))" | cut -d' ' -f2- \
    | while IFS= read -r old; do
        case "$old" in "$VENV_CACHE"/*) rm -rf "$old" ;; esac
      done
}

VENV_ERROR=""
resolve_venv() {
  local t0=$SECONDS want have dir
  if [ -n "${GATE_VENV:-}" ]; then
    echo "venv: GATE_VENV=$VENV (explicit; the venv step checks it against the pins)"
    return 0
  fi
  want="$(req_hash "$REPO_ROOT")"
  have="$(req_hash "$MAIN_ROOT")"
  if [ "$want" = "$have" ] && venv_in_sync "$MAIN_VENV"; then
    VENV="$MAIN_VENV"
    echo "venv: main venv (requirements hash $want matches, installed == pins)"
  elif [ "$REPO_ROOT" = "$MAIN_ROOT" ]; then
    # Never rebuilt behind the developer's back. Every step fails (VENV_ERROR), not only the
    # venv step: `--only pytest` must not pass against drifted packages (DA-B2-2).
    VENV="$MAIN_VENV"
    VENV_ERROR="the main venv ($MAIN_VENV) does not match this tree's pins — fix with: $MAIN_VENV/bin/pip install -r backend/requirements-dev.txt"
    echo "venv: main venv is out of sync with the pins"
  else
    dir="$VENV_CACHE/$want"
    if [ ! -f "$dir/.secrag-complete" ]; then
      echo "venv: pins differ from the main venv → building cached venv $want (budget ${VENV_BUILD_BUDGET}s)"
      timeout -k 10 "$VENV_BUILD_BUDGET" bash "$SELF" --build-venv "$dir" 2>&1 | sed 's/^/  /'
      if [ "${PIPESTATUS[0]}" -ne 0 ] || [ ! -f "$dir/.secrag-complete" ]; then
        VENV_ERROR="could not build the gate venv for requirements hash $want"
      fi
    fi
    [ -n "$VENV_ERROR" ] || touch "$dir/.secrag-complete"   # last use, for pruning
    VENV="$dir"
    echo "venv: cached venv $dir ($((SECONDS - t0))s)"
    prune_venv_cache
  fi
  PY="$VENV/bin/python"
  # Steps run as `bash gate.sh --run-step`, which re-derives VENV from GATE_VENV.
  GATE_VENV="$VENV"
  export VENV PY GATE_VENV
}

# --- gate project (stack) ---------------------------------------------------------------

STACK_STARTED=0
STACK_ERROR=""   # gate DB unusable (db and seed steps fail)
SEED_ERROR=""    # seed missing / stale / not restored (seed steps fail)

gate_urls() { # gate_urls <password>
  export DATABASE_URL="postgresql+psycopg://$GATE_DB_USER:$1@127.0.0.1:$GATE_DB_PORT/$GATE_DB_NAME"
  # Harness maintenance DB on the gate server; the harness creates its own test databases.
  export TEST_DATABASE_URL="postgresql+psycopg://$GATE_DB_USER:$1@127.0.0.1:$GATE_DB_PORT/postgres"
}

resolve_ollama_host() {
  local from_env=""
  if [ -n "${GATE_OLLAMA_HOST:-}" ]; then echo "$GATE_OLLAMA_HOST"; return; fi
  if [ -n "${OLLAMA_HOST:-}" ]; then echo "$OLLAMA_HOST"; return; fi
  [ -f "$MAIN_ROOT/.env" ] && from_env="$(sed -n 's/^OLLAMA_HOST=//p' "$MAIN_ROOT/.env" | tail -1)"
  echo "${from_env:-http://127.0.0.1:11434}"
}

ollama_reachable() { curl -4 -fsS -m 5 -o /dev/null "${OLLAMA_HOST%/}/api/tags" 2>/dev/null; }

# "<model> <digest>" of the embedding model Ollama serves now (DA-B-9); empty on error.
embed_model_digest() {
  (cd "$REPO_ROOT/backend" && "$PY" -m rag_app.devtools.ollama_digest --host "$OLLAMA_HOST" 2>/dev/null)
}

db_alembic_head() {
  compose exec -T db psql -U "$GATE_DB_USER" -d "$GATE_DB_NAME" -tAc 'SELECT version_num FROM alembic_version'
}

# Local passwords of the database roles (git-ignored gate state, never printed). Created once;
# the gate cluster is thrown away after every run, so they only need to be stable per clone.
ensure_roles_env() {
  [ -f "$ROLES_ENV" ] && return 0
  mkdir -p "$GATE_STATE" || return 1
  chmod 700 "$GATE_STATE" || return 1
  (
    umask 077
    {
      echo "# Local passwords for db/roles.sql roles (scripts/db/apply_roles.sh). Git-ignored."
      echo "SECRAG_PURGER_PASSWORD=$(openssl rand -hex 24)"
      echo "SECRAG_BACKUP_PASSWORD=$(openssl rand -hex 24)"
    } >"$ROLES_ENV.tmp" && mv -f "$ROLES_ENV.tmp" "$ROLES_ENV"
  )
}

stack_down() {
  [ "$STACK_STARTED" = 1 ] || return 0
  echo ""
  echo "== gate project: down -v ($COMPOSE_PROJECT only) =="
  GATE_DB_PASSWORD="${GATE_DB_PASSWORD:-unused}" compose down -v --remove-orphans 2>&1 | sed 's/^/  /'
  STACK_STARTED=0
}

# stack_up <seed: 1/0>: start the gate DB, migrate, restore the seed.
# Sets STACK_ERROR (DB unusable) or SEED_ERROR (DB fine, seed not restored).
stack_up() {
  local want_seed="$1" config
  if ! docker info >/dev/null 2>&1; then
    STACK_ERROR="docker is not reachable from WSL — start Docker Desktop (the gate project needs it)"
    return 1
  fi
  GATE_DB_PASSWORD="$(openssl rand -hex 16)"
  export GATE_DB_PASSWORD
  config="$(compose config --format json)" || { STACK_ERROR="compose.gate.yml does not resolve"; return 1; }
  if ! "$PY" -m rag_app.devtools.gate_compose <<<"$config"; then
    STACK_ERROR="compose.gate.yml is not isolated from the development stack (see above)"
    return 1
  fi
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$GATE_DB_PORT\$"; then
    # A leftover gate project from a killed run is ours to remove; anything else is not.
    if [ -n "$(compose ps -aq 2>/dev/null)" ]; then
      compose down -v --remove-orphans >/dev/null 2>&1
    else
      STACK_ERROR="port $GATE_DB_PORT is in use by something else"
      return 1
    fi
  fi
  STACK_STARTED=1
  compose down -v --remove-orphans >/dev/null 2>&1   # stale volume from a killed run
  compose up -d --wait db 2>&1 | sed 's/^/  /'
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then STACK_ERROR="gate DB did not become healthy"; return 1; fi
  gate_urls "$GATE_DB_PASSWORD"
  echo "  gate DB up on 127.0.0.1:$GATE_DB_PORT (project $COMPOSE_PROJECT, volume secrag_gate_pgdata)"
  # Roles before migrate, as in compose, CI and Azure (T11.0.13).
  ensure_roles_env || { STACK_ERROR="could not create $ROLES_ENV"; return 1; }
  if ! command -v psql >/dev/null; then
    STACK_ERROR="psql not found (scripts/prereqs/install.sh pg) — needed to apply db/roles.sql"
    return 1
  fi
  # shellcheck disable=SC1090
  if ! (set -a; . "$ROLES_ENV"; set +a
        PGPASSWORD="$GATE_DB_PASSWORD" bash "$REPO_ROOT/scripts/db/apply_roles.sh" \
          "postgresql://$GATE_DB_USER@127.0.0.1:$GATE_DB_PORT/$GATE_DB_NAME" 2>&1 | sed 's/^/  /'
        exit "${PIPESTATUS[0]}"); then
    STACK_ERROR="db/roles.sql failed on the gate DB"
    return 1
  fi
  if ! (cd backend && "$VENV/bin/alembic" upgrade head 2>&1 | sed 's/^/  /'; exit "${PIPESTATUS[0]}"); then
    STACK_ERROR="alembic upgrade head failed on the gate DB"
    return 1
  fi
  [ "$want_seed" = 1 ] || return 0
  if [ ! -f "$SEED" ]; then
    SEED_ERROR="no gate seed at $SEED — run: scripts/gate.sh --make-seed (needs Ollama)"
    return 2
  fi
  # Staleness (DA-B-9): corpus, embedding model build and schema must match the seed.
  local seeded_hash current_hash seeded_model current_model seeded_head current_head
  seeded_hash="$(sed -n 's/^corpus_sha256=//p' "$SEED_META" 2>/dev/null)"
  if [ -f "$CHUNKS" ]; then
    current_hash="$(sha256sum "$CHUNKS" | cut -d' ' -f1)"
    if [ "$seeded_hash" != "$current_hash" ]; then
      SEED_ERROR="the corpus changed since the seed was made — run: scripts/gate.sh --make-seed"
      return 2
    fi
  fi
  seeded_model="$(sed -n 's/^embed_model=//p' "$SEED_META" 2>/dev/null)"
  if [ -z "$seeded_model" ]; then
    SEED_ERROR="the seed does not record its embedding model digest (older seed) — run: scripts/gate.sh --make-seed"
    return 2
  fi
  if ollama_reachable; then   # unreachable: eval fails on its own with a clear message
    current_model="$(embed_model_digest)"
    if [ "$seeded_model" != "$current_model" ]; then
      SEED_ERROR="the embedding model changed since the seed was made (seed: $seeded_model; Ollama now: ${current_model:-not served}) — run: scripts/gate.sh --make-seed"
      return 2
    fi
  fi
  seeded_head="$(sed -n 's/^alembic_head=//p' "$SEED_META" 2>/dev/null)"
  current_head="$(db_alembic_head)"
  if [ "$seeded_head" != "$current_head" ]; then
    SEED_ERROR="migrations changed since the seed was made (seed at ${seeded_head:-?}, head ${current_head:-?}) — run: scripts/gate.sh --make-seed"
    return 2
  fi
  if ! compose exec -T db pg_restore -U "$GATE_DB_USER" -d "$GATE_DB_NAME" \
         --data-only --disable-triggers --no-owner <"$SEED"; then
    SEED_ERROR="seed restore failed"
    return 2
  fi
  echo "  seed restored: $(sed -n 's/^chunks=//p' "$SEED_META") chunks ($(sed -n 's/^created=//p' "$SEED_META"); $seeded_model; alembic $seeded_head)"
}

make_seed() {
  export GATE_MODE=full
  local t0=$SECONDS
  [ -f "$CHUNKS" ] || { echo "no corpus at $CHUNKS — build it first (README §4)"; return 1; }
  export OLLAMA_HOST
  if ! ollama_reachable; then
    echo "Ollama not reachable at $OLLAMA_HOST — start it (native: ollama serve) or set GATE_OLLAMA_HOST"
    return 1
  fi
  local model
  model="$(embed_model_digest)"
  [ -n "$model" ] || { echo "  FAIL: the embedding model is not served by $OLLAMA_HOST (ollama pull it)"; return 1; }
  trap 'stack_down' EXIT
  trap 'exit 130' INT TERM
  echo "== make-seed: gate project up =="
  stack_up 0 || { echo "  FAIL: $STACK_ERROR"; return 1; }
  echo "== make-seed: index $CHUNKS (embeddings via $OLLAMA_HOST, $model) =="
  (cd backend && "$PY" -m rag_app.indexing --chunks "$CHUNKS") || { echo "  FAIL: indexing"; return 1; }
  [ "$(embed_model_digest)" = "$model" ] \
    || { echo "  FAIL: the embedding model changed while indexing — run --make-seed again"; return 1; }
  mkdir -p "$GATE_STATE"
  chmod 700 "$GATE_STATE"
  compose exec -T db pg_dump -U "$GATE_DB_USER" -d "$GATE_DB_NAME" -Fc --data-only \
    -t documents -t chunks >"$SEED.tmp" || { echo "  FAIL: pg_dump"; rm -f "$SEED.tmp"; return 1; }
  local chunks head
  chunks="$(compose exec -T db psql -U "$GATE_DB_USER" -d "$GATE_DB_NAME" -tAc 'SELECT count(*) FROM chunks')"
  head="$(db_alembic_head)"
  mv -f "$SEED.tmp" "$SEED"
  {
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "corpus_sha256=$(sha256sum "$CHUNKS" | cut -d' ' -f1)"
    echo "embed_model=$model"
    echo "alembic_head=$head"
    echo "chunks=$chunks"
  } >"$SEED_META"
  echo "seed written: $SEED ($chunks chunks, $model, alembic $head) in $((SECONDS - t0))s"
}

# --- secrag/gate-full commit status (D-2026-09-27-7 b) ---------------------------------

# publish_gate_status <mode> <fail> <seconds>: every gate run ends here; only a --full PASS
# on a clean, unmoved tree posts `secrag/gate-full` = success for exactly the checked SHA
# (CD refuses a SHA without it). The decision and the detached publisher for the pre-push
# case live in scripts/cd/gate_publish.sh (tested, DA-C-4). Never changes the gate result.
publish_gate_status() {
  local mode="$1" fail="$2" total="$3" result=FAIL
  [ "$fail" -eq 0 ] && result=PASS
  bash "$REPO_ROOT/scripts/cd/gate_publish.sh" --mode "$mode" --result "$result" \
    --start-sha "$GATE_START_SHA" --start-dirty "$GATE_START_DIRTY" --total "$total" \
    --state-dir "$GIT_COMMON/secrag-gate" || true
}

# --- runner -----------------------------------------------------------------------------

declare -a RESULTS=()
fail=0

run_step() {
  local name="$1" budget t0 rc status
  budget="$(step_field "$name" 3)"
  echo ""
  echo "== $name (budget ${budget}s) =="
  t0=$SECONDS
  local need
  need="$(step_field "$name" 2)"
  if { [ "$need" = db ] || [ "$need" = seed ]; } && [ -n "$STACK_ERROR" ]; then
    echo "  FAIL: stack not available — $STACK_ERROR"
    rc=1
  elif [ "$need" = seed ] && [ -n "$SEED_ERROR" ]; then
    echo "  FAIL: gate seed not available — $SEED_ERROR"
    rc=1
  else
    timeout -k 10 "$budget" bash "$SELF" --run-step "$name"
    rc=$?
  fi
  local elapsed=$((SECONDS - t0))
  case "$rc" in
    0) status=PASS ;;
    3) status=SKIP ;;
    124 | 137) status=FAIL; echo "  FAIL: exceeded the ${budget}s budget" ;;
    *) status=FAIL ;;
  esac
  [ "$status" = FAIL ] && fail=1
  echo "  $status (${elapsed}s)"
  RESULTS+=("$(printf '%-20s %-5s %5ss / %ss' "$name" "$status" "$elapsed" "$budget")")
}

usage() { sed -n '2,25p' "$SELF" | sed 's/^# \{0,1\}//'; }

main() {
  local mode=fast only="" arg
  while [ $# -gt 0 ]; do
    arg="$1"
    case "$arg" in
      --run-step) shift; "step_$1"; return ;;
      --build-venv) shift; build_venv "$1"; return ;;
      --fast) mode=fast ;;
      --full) mode=full ;;
      --only) shift; mode=only; only="$only,${1:-}" ;;
      --only=*) mode=only; only="$only,${arg#--only=}" ;;
      --make-seed) mode=seed ;;
      --list)
        echo "step             stack budget(s)"
        awk 'NF==3' <<<"$STEP_TABLE"
        return 0 ;;
      -h | --help) usage; return 0 ;;
      *) echo "unknown argument: $arg" >&2; usage >&2; return 2 ;;
    esac
    shift
  done

  # Native steps only ever see gate endpoints: the gate DB port (nothing listens there
  # until the gate project is up) and the gate's Ollama. Never the dev DB on 5432.
  export DATABASE_URL="postgresql+psycopg://$GATE_DB_USER@127.0.0.1:$GATE_DB_PORT/$GATE_DB_NAME"
  unset TEST_DATABASE_URL
  OLLAMA_HOST="$(resolve_ollama_host)"
  export OLLAMA_HOST

  local steps=() name
  case "$mode" in
    fast) mapfile -t steps < <(awk 'NF==3 && $2=="-" {print $1}' <<<"$STEP_TABLE") ;;
    full) mapfile -t steps < <(step_names) ;;
    only)
      IFS=, read -ra steps <<<"${only#,}"
      for name in "${steps[@]}"; do
        is_step "$name" || { echo "unknown step: $name (see --list)" >&2; return 2; }
      done ;;
  esac
  case "$mode" in
    full | only) export GATE_MODE="$mode" ;;
    *) export GATE_MODE=fast ;;
  esac

  echo "gate: mode=$mode, tree=$REPO_ROOT${REPO_ROOT:+ @ $(git rev-parse --short HEAD 2>/dev/null)}"
  # The commit a --full PASS may vouch for: fixed now, re-checked before publishing.
  GATE_START_SHA="$(git rev-parse HEAD 2>/dev/null)"
  GATE_START_DIRTY=1
  [ -z "$(git status --porcelain 2>/dev/null)" ] && GATE_START_DIRTY=0
  local t0=$SECONDS v0=$SECONDS
  resolve_venv
  if [ -n "$VENV_ERROR" ]; then
    echo "  FAIL: $VENV_ERROR"
    fail=1
  fi
  RESULTS+=("$(printf '%-20s %-5s %5ss' venv-resolve "$([ -z "$VENV_ERROR" ] && echo PASS || echo FAIL)" "$((SECONDS - v0))")")

  if [ "$mode" = seed ]; then
    [ -z "$VENV_ERROR" ] || return 1
    make_seed
    return
  fi

  local needs_stack=0
  for name in "${steps[@]}"; do
    case "$(step_field "$name" 2)" in
      db) [ "$needs_stack" = 0 ] && needs_stack=db ;;
      seed) needs_stack=seed ;;
    esac
  done

  trap 'stack_down' EXIT
  trap 'exit 130' INT TERM
  if [ "$needs_stack" != 0 ]; then
    echo ""
    echo "== gate project: up → migrate → seed (budget ${STACK_BUDGET}s) =="
    local s0=$SECONDS
    stack_up "$([ "$needs_stack" = seed ] && echo 1 || echo 0)"
    [ -n "$STACK_ERROR$SEED_ERROR" ] && echo "  FAIL: $STACK_ERROR$SEED_ERROR"
    if [ "$needs_stack" = seed ] && [ -z "$STACK_ERROR" ] && ! ollama_reachable; then
      echo "  note: Ollama not reachable at $OLLAMA_HOST (eval will fail) — start it or set GATE_OLLAMA_HOST"
    fi
    if [ $((SECONDS - s0)) -gt "$STACK_BUDGET" ]; then
      echo "  FAIL: stack setup exceeded ${STACK_BUDGET}s"
      STACK_ERROR="${STACK_ERROR:-stack setup over budget}"
    fi
    local stack_status=PASS
    [ -n "$STACK_ERROR$SEED_ERROR" ] && { stack_status=FAIL; fail=1; }
    RESULTS+=("$(printf '%-20s %-5s %5ss / %ss' stack "$stack_status" "$((SECONDS - s0))" "$STACK_BUDGET")")
  fi

  for name in "${steps[@]}"; do
    run_step "$name"
  done
  stack_down

  local total=$((SECONDS - t0))
  echo ""
  echo "============================================"
  printf '%s\n' "${RESULTS[@]}"
  echo "--------------------------------------------"
  echo "total ${total}s (mode $mode) @ $(git rev-parse HEAD 2>/dev/null)"
  if [ "$mode" = full ] && [ "$total" -gt "$FULL_TARGET_SECONDS" ]; then
    echo "WARNING: --full took ${total}s, over the ${FULL_TARGET_SECONDS}s target (X5)"
  fi
  if [ "$fail" -eq 0 ]; then
    echo "GATE: PASS"
  else
    echo "GATE: FAIL — see failures above (docs/DEFINITION_OF_DONE.md)"
  fi
  echo "============================================"
  publish_gate_status "$mode" "$fail" "$total"
  return "$fail"
}

main "$@"
