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
# Gate log (C8c, T11c.E1): every real run's full output is also written to a file under
# GATE_LOG_DIR (default ~/secrag-gate-logs) -- never under /tmp, which is tmpfs and is wiped
# when WSL stops, losing the "--full PASS log for the exact SHA" a promotion needs
# (docs/DEFINITION_OF_DONE.md "Promotion evidence"). The directory is not pruned here; it is
# plain evidence the developer can clean up any time. --list/-h/--help and the script's own
# re-entrant calls (--run-step, --build-venv) never open a log file.
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

# --- F24 guard: refuse a manual-background launch (DA-13-HI-2, 2026-11-25) -------------
# `cmd &`, `nohup cmd &` and `( cmd & )` in a non-interactive, non-job-control shell make
# bash set BOTH SIGINT and SIGQUIT to ignored for that backgrounded command (POSIX
# "asynchronous list" semantics) -- and once ignored, that disposition survives every
# exec() this process or its children do (an exec() only resets signals that are *caught*
# back to default; SIG_IGN is preserved), which is exactly what broke the
# test_db_tunnel.py signal tests three times on 2026-11-25 (F24). A plain foreground
# invocation (incl. this script's own `bash "$SELF" --run-step ...` re-entrant children,
# the pre-push hook, CI's `gate.sh --only ...`, and the Bash-tool's own
# run_in_background mechanism -- verified empirically: it does not set SIGINT to
# ignored, only this environment's own baseline SIGQUIT-ignored quirk, which alone never
# trips this check) never has SIGINT ignored, so checking SIGINT *and* SIGQUIT together
# (not either alone) tells a real manual background apart from every sanctioned launch.
# Read from /proc instead of `trap -p` so the check is independent of anything this
# script traps later and survives even if bash's own trap bookkeeping doesn't surface an
# inherited (pre-exec) ignore. Escape hatch for a deliberate, understood use:
# GATE_BACKGROUND_OK=1 (named in the F24 failures.md note).
check_not_backgrounded() {
  [ -n "${GATE_BACKGROUND_OK:-}" ] && return 0
  local sigign status_file="/proc/$$/status" hexval
  [ -r "$status_file" ] || return 0   # no /proc (non-Linux) -- cannot check, don't block
  sigign="$(awk '/^SigIgn:/{print $2}' "$status_file" 2>/dev/null)"
  [ -n "$sigign" ] || return 0
  hexval=$((16#$sigign)) 2>/dev/null || return 0
  # SIGINT=2 -> bit 0x2, SIGQUIT=3 -> bit 0x4; manual backgrounding sets BOTH.
  if [ $((hexval & 0x6)) -eq 6 ]; then
    echo "FAIL (F24): scripts/gate.sh refuses to start — SIGINT and SIGQUIT are both" >&2
    echo "  ignored, which means this shell launched it as a manual background job" >&2
    echo "  (\`cmd &\`, \`nohup cmd &\`, or \`( cmd & )\`). That changes signal disposition" >&2
    echo "  for every child process (incl. psql) and breaks test_db_tunnel.py's signal" >&2
    echo "  tests (DA-13-HI-2). Run it in the foreground, via the pre-push hook, via CI," >&2
    echo "  or via the sanctioned tool's own background-run mechanism instead." >&2
    echo "  Deliberate override: GATE_BACKGROUND_OK=1 scripts/gate.sh ..." >&2
    exit 1
  fi
}
check_not_backgrounded

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
# F31 (2026-11-25): one seed per alembic head under $GATE_STATE/seeds/<head>/{seed.dump,
# seed.meta} -- $GATE_STATE is shared across every worktree (MAIN_ROOT), so a single
# top-level seed.dump/seed.meta broke every branch whose alembic head differed from
# whichever branch last ran --make-seed (14-C3 rebuilt it at 0009_conversation_summary,
# failing eval/latency/e2e on every 0008 branch, main included). seed_dir_for_head()
# below resolves the right directory; SEED/SEED_META (legacy top-level path, pre-F31) are
# kept only as a read-only fallback for a tree whose head still matches them -- --make-seed
# never writes there again.
SEEDS_DIR="$GATE_STATE/seeds"
SEED="$GATE_STATE/seed.dump"
SEED_META="$GATE_STATE/seed.meta"
CHUNKS="$MAIN_ROOT/data/chunks/chunks.jsonl"

# Venvs for trees whose pins differ from the main venv (DA-B-4).
VENV_CACHE="$GIT_COMMON/secrag-gate/venvs"
VENV_RECIPE=2                     # bump when build_venv changes (2: torch from requirements-torch.txt)
VENV_BUILD_BUDGET=900
VENV_CACHE_KEEP=2                 # ~1.5 GB each (torch)

COMPOSE_PROJECT="secrag-gate"
COMPOSE_FILE="$REPO_ROOT/compose.gate.yml"
# Below the Windows dynamic range: Hyper-V reserves blocks of 49152-65535 (55432 failed).
GATE_DB_PORT="${GATE_DB_PORT:-15432}"
export GATE_DB_PORT
GATE_DB_USER="secrag_gate"
GATE_DB_NAME="secrag_gate"
# T12.4.4: the gate's own Mailpit (compose.gate.yml), brought up on demand by
# e2e_start_backend -- different host ports from the dev stack's Mailpit (8025/1025) so both
# can run at once.
GATE_MAILPIT_SMTP_PORT="${GATE_MAILPIT_SMTP_PORT:-11025}"
GATE_MAILPIT_API_PORT="${GATE_MAILPIT_API_PORT:-18026}"
export GATE_MAILPIT_SMTP_PORT GATE_MAILPIT_API_PORT
FULL_TARGET_SECONDS=1800   # X5: --full target in Phase 11

# --- global gate lock (T13.14.4, Paralelo-B problem #2) --------------------------------
# One scripts/gate.sh run at a time, any mode, incl. the pre-push path: concurrent gates
# share the same fixed compose project/ports/volume (COMPOSE_PROJECT, GATE_DB_PORT, ...)
# and the same native Ollama, and would corrupt each other (down -v races, port clashes).
# scripts/dev/with-lock.sh takes the SAME lock for an agent's own Docker/e2e/dev-server
# runs outside gate.sh. Released automatically on process exit or kill -9 (flock/fd
# semantics on the held file descriptor -- no trap needed). Plain unit tests (no Docker)
# never need this lock. GATE_LOCK_TEST_SLEEP is a test-only hook (default 0 = no-op): the
# real code path never sets it. Only GATE_LOCK_FILE's own directory is ever created (never
# a separately-defaulted GATE_LOCK_DIR) so an explicit override never touches $HOME.
GATE_LOCK_FILE="${GATE_LOCK_FILE:-${XDG_RUNTIME_DIR:-$HOME/.cache}/secrag/gate.lock}"
GATE_LOCK_HOLDER_FILE="$GATE_LOCK_FILE.holder"
GATE_LOCK_TIMEOUT="${GATE_LOCK_TIMEOUT:-3600}"   # seconds; 60 min default, configurable
GATE_LOCK_TEST_SLEEP="${GATE_LOCK_TEST_SLEEP:-0}"

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
private-names    - 30
threat-catalogue - 30
registry         - 60
frontend         - 600
dependency-audit - 240
db-tests         db 300
security-suite   db 180
defence-suite    db 240
migrations-roundtrip db 180
backup-drill     db 300
erasure-scale    db 600
restart-check    docker 900
eval             seed 1200
latency          seed 900
e2e              seed 600
security-headers db 300
zap-baseline     db 600
email-e2e        db 180
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
  [ "${PIPESTATUS[0]}" -eq 0 ] || { echo "  FAIL: fix with: $VENV/bin/pip install -r backend/requirements-torch.txt, then -r backend/requirements-dev.txt"; return 1; }
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

# DEBT-026: no public, tracked Markdown file names a private planning file.
step_private-names() { need_venv && "$PY" -m rag_app.devtools.private_names --root "$REPO_ROOT"; }

# T12.0.5: every THREAT_MODEL.md catalogue entry has a phase + test ID; mitigated entries
# point at a test that exists.
step_threat-catalogue() { need_venv && "$PY" -m rag_app.devtools.threat_catalogue --root "$REPO_ROOT"; }

# T13.0.5: router tags/metrics/modules match rag_app.features.REGISTRY; every health probe
# answers <= 2s. T14.5.3 (DEBT-049): the single-item misspelling quality probe is now
# must-pass -- it FAILS the step whenever a live DB is reachable (the gate's own
# DATABASE_URL -- a stopped port in --fast, the real gate DB once --full/--only has brought
# the stack up) and the probe itself comes back "failed" (a genuine regression); the harder
# `misspelling-tolerance-variants` probe stays a tracked, non-blocking gap (DEBT-049).
step_registry() { need_venv && "$PY" -m rag_app.devtools.registry_check --root "$REPO_ROOT"; }

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
  (cd frontend && npm run lint && npm run typecheck && npm test && npm run build)
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
  # "and not security": backend/tests/security/ is also marked `db` but runs as its OWN
  # step below (T12.1.0) -- never both, so nothing runs twice.
  (cd backend && SECRAG_REQUIRE_DB_TESTS=1 "$VENV/bin/pytest" -m "db and not security" -p no:cacheprovider -rs)
}

# T12.1.0: backend/tests/security/ — sessions, cookie auth (12a), later CSRF/route-walker
# (12.1/12.2), roles/MFA (12b). Needs the gate DB like db-tests; kept as its own step so a red
# security test is never buried among the much larger db-tests run.
step_security-suite() {
  need_venv || return 1
  (cd backend && SECRAG_REQUIRE_DB_TESTS=1 "$VENV/bin/pytest" -m security -p no:cacheprovider -rs)
}

# T13.3.6: every THREAT_MODEL-catalogue-keyed defence probe (rag_app.defence_lab) must match
# its expected outcome -- 100%, no partial credit. Same TEST_DATABASE_URL/DATABASE_URL the
# gate stack already exports for security-suite/db-tests above (rag_app.defence_lab's own
# module docstring has the full "fixed local target" reasoning); the erasure-at-scale probe
# (DEBT-005) is deliberately its OWN separate step (erasure-scale, below) -- it needs a
# throwaway Postgres SUPERUSER connection this step does not have.
step_defence-suite() {
  need_venv || return 1
  (cd "$REPO_ROOT" && "$PY" -m rag_app.devtools.defence_lab_cli run 2>&1 | sed 's/^/  /'; exit "${PIPESTATUS[0]}")
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
# install_drill_azure_extension: a stand-in for Azure's `azure` extension in the gate DB
# container (thrown away with it): only a session with secrag.drill_azure_source=on (the
# drill's source database) can create it; anywhere else CREATE EXTENSION fails like
# "extension azure is not available" on a non-Azure server — so a dump of the source
# carries the same TOC entries as a real Azure dump.
install_drill_azure_extension() {
  # shellcheck disable=SC2016  # $(pg_config …) expands inside the container
  local into='cat >"$(pg_config --sharedir)/extension/$1"'
  compose exec -T db sh -c "$into" _ azure.control <<'EOF' || return 1
comment = 'secrag backup-drill stand-in for the Azure-only extension'
default_version = '1.0'
relocatable = false
schema = pg_catalog
superuser = true
EOF
  compose exec -T db sh -c "$into" _ azure--1.0.sql <<'EOF'
DO $$
BEGIN
  IF current_setting('secrag.drill_azure_source', true) IS DISTINCT FROM 'on' THEN
    RAISE EXCEPTION 'extension "azure" is not available (backup-drill stand-in: only the drill source may create it)';
  END IF;
END
$$;
EOF
}

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
  install_drill_azure_extension || { echo "  FAIL: cannot install the stand-in azure extension in the gate DB"; return 1; }
  DRILL_ADMIN_URL="postgresql://$GATE_DB_USER:$GATE_DB_PASSWORD@127.0.0.1:$GATE_DB_PORT/postgres" \
    SECRAG_PYTHON="$PY" bash "$REPO_ROOT/scripts/db/backup_drill.sh" 2>&1 | sed 's/^/  /'
  return "${PIPESTATUS[0]}"
}

# T11.2b.7: a synthetic user with 100,000 messages erased under concurrent login + listing
# load (the real API in a child process, a throwaway secrag_scale_* database on the gate
# server): request < 200 ms, pool never exhausted, no lock error reaches other requests; the
# purger as secrag_purger is killed mid-way, an overlapping run is skipped, the next run
# leaves the tombstone done (rag_app.devtools.erasure_scale).
step_erasure-scale() {
  need_venv || return 1
  [ -f "$ROLES_ENV" ] || { echo "  FAIL: $ROLES_ENV missing (created by the gate stack)"; return 1; }
  command -v psql >/dev/null || { missing_tool "psql 16 (scripts/prereqs/install.sh pg)"; return; }
  set -a
  # shellcheck source=/dev/null
  . "$ROLES_ENV"
  set +a
  (cd "$REPO_ROOT/backend" \
    && SCALE_ADMIN_URL="postgresql://$GATE_DB_USER:$GATE_DB_PASSWORD@127.0.0.1:$GATE_DB_PORT/postgres" \
       env -u DATA_MASTER_KEY -u JWT_SECRET "$PY" -m rag_app.devtools.erasure_scale run 2>&1 \
    | sed 's/^/  /'; exit "${PIPESTATUS[0]}")
}

# T11.2.8: data survives down/up (no -v) even across compose project names (fixed external
# volume, T11.2.1); the backend image never migrates (T11.2.5); a changed master key refuses
# to start (T11.2.4, DA-D-2); the dedicated account is erased at the end. Throwaway projects
# `secrag-gate-rc*` and a throwaway external volume, API on 127.0.0.1:18000 (no DB port) —
# never the development volume, and it can run with the development stack up.
step_restart-check() {
  docker info >/dev/null 2>&1 \
    || { echo "  FAIL: docker is not reachable from WSL — start Docker Desktop"; return 1; }
  env -u JWT_SECRET -u CSRF_SECRET -u CLIENT_IP_HMAC_KEYS -u DATA_MASTER_KEY -u ENV bash "$REPO_ROOT/scripts/restart_check.sh" \
    --project secrag-gate-rc --restart-project secrag-gate-rc2 --restart-master-key 2>&1 \
    | sed 's/^/  /'
  return "${PIPESTATUS[0]}"
}

step_eval() {
  need_venv || return 1
  local extra=()
  # shellcheck disable=SC2206 # our own space-separated flag list (one-off runs only, e.g.
  # EVAL_EXTRA_ARGS="--update-baseline" after T11.5.3 adopts a new reranker config)
  [ -n "${EVAL_EXTRA_ARGS:-}" ] && extra=($EVAL_EXTRA_ARGS)
  # The reranker must come from the local cache at its pinned revision (row 37d): offline.
  (cd backend && HF_HUB_OFFLINE=1 "$PY" -m rag_app.eval.gate --require-stack "${extra[@]}")
}

# T11.3.5/T11.4.4/T11.6b.1: records p50/p95 latency per stage over the golden set and compares
# it against the ADOPTED baseline (eval/latency_adopted_baseline.json, git-tracked, written
# only via --update-adopted-baseline) -- a real pass/fail step. Writes the git-ignored
# eval/latency_results.json every run (eval/latency_baseline.json, the FROZEN "before", is
# untouched unless --update-baseline is passed explicitly via LATENCY_EXTRA_ARGS). Same
# offline-reranker requirement as `eval`.
# OMP_NUM_THREADS/MKL_NUM_THREADS=2 (T11.6b.1): pins every invocation of this module to 2
# threads, matching Azure's 2-vCPU container and the number the absolute rerank floor (<=1.5s,
# DA-11bE-2) is judged on -- must be set in the environment before the interpreter starts
# (native libs read these at load time); rag_app.eval.latency.main() ALSO calls
# torch.set_num_threads(2) defensively. This makes the step's own numbers host-independent
# (dev box, CI runner, Azure all measure the SAME thing), not just the matrix screening.
# LATENCY_EXTRA_ARGS (opt-in, unset by default — a routine run is unaffected): extra flags for
# ad-hoc one-off measurements against the isolated gate stack (T11.4.4's --reranker-mode
# warm-single "before, warm" reference, --warm-vs-cold, --concurrency N, or T11.6b.1's
# --update-adopted-baseline), never used by a routine `--fast`/`--full`/CI run.
step_latency() {
  need_venv || return 1
  local extra=()
  # shellcheck disable=SC2206 # our own space-separated flag list (ad-hoc one-off runs only)
  [ -n "${LATENCY_EXTRA_ARGS:-}" ] && extra=($LATENCY_EXTRA_ARGS)
  (cd backend && HF_HUB_OFFLINE=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
    "$PY" -m rag_app.eval.latency --require-stack "${extra[@]}")
}

# T12.1.10/T12.2.5/T12.2.6: a NATIVE backend (uvicorn, against the gate DB/Ollama main()
# already exported) and the built frontend (`next start`, not the Docker `standalone` output
# -- these steps run the app directly, never the image) as plain background processes on
# throwaway loopback ports. Three gate steps share this ONE pair of helpers --
# `step_e2e`/`step_security-headers`/`step_zap-baseline` -- never running concurrently (each
# step is its own `bash "$SELF" --run-step <name>` subprocess, run_step/main's `for name in
# steps` loop is sequential), so the shared globals below never collide across them. ENV=dev
# (no SMTP, no cap on verification) so register → login needs no email step.
E2E_BACKEND_PORT="${E2E_BACKEND_PORT:-18100}"
E2E_FRONTEND_PORT="${E2E_FRONTEND_PORT:-18101}"
E2E_BACKEND_PID=""
E2E_FRONTEND_PID=""
E2E_CLIENT_IP_KEY=""   # set by e2e_start_backend; e2e_start_frontend must reuse the SAME value
# T13.2.10: one known admin test account + one known plain-user erasure TARGET, both seeded
# straight into the database (never a route -- role is never writable through any route,
# T12.3.3) by step_e2e only (not the other two steps, which have no use for them). Emails
# are plain fixture constants (not secrets, not real PII); the (shared) password lives in
# E2E_ENV below, same throwaway-secret pattern as every other E2E_* value there.
# DA-13-C-1: the erasure target is SEEDED, never registered through the UI -- one more
# `/auth/register` call would spend more of rag_app.risk.SignupTracker's one-hour, per-IP
# signup-velocity budget that auth/admin/mfa.spec.ts's own throwaway registrations already
# leave little headroom in (found for real: a 6th registration in one run tripped it).
E2E_ADMIN_EMAIL="e2e-admin@example.test"
# DEBT-051/DEC-041 (13-F1): admin-dashboard.spec.ts's SECOND test reuses the admin account
# but `enrolAndStepUpMfa` always (re-)enrols from scratch -- since `/account/mfa/enroll` on
# an already-ENABLED record now needs proof of the current factor (this block's whole
# point), a second from-scratch enrolment through the UI, which never supplies one, would
# 403. Giving that test its OWN seeded admin, never touched by the first test, is simpler
# than threading a TOTP secret across two otherwise-independent Playwright tests -- the
# full step-up-only (no re-enrol) UI path is 13-F2's own e2e addition.
E2E_ADMIN2_EMAIL="e2e-admin2@example.test"
E2E_ERASE_TARGET_EMAIL="e2e-erase-target@example.test"
# T13.7.5/DEBT-051: seeded directly (never `registerAndReachChat`, same DA-13-C-1 reasoning
# as ERASE_TARGET_EMAIL above) so the "a plain user never sees Admin panel" e2e check never
# adds to the real `/auth/register` signup-velocity budget (`rag_app.risk`, 5 signups/hour/IP
# -- this step's own backend process is the velocity tracker's single in-memory instance for
# every test below, so one extra registration here used to intermittently 403-block the
# UNRELATED Mailpit verify test's registration, found running this block for real).
E2E_PLAIN_USER_EMAIL="e2e-plain-user@example.test"
# T13.9.2 (DEBT-056): wakeup.spec.ts's own plain user, seeded directly for the SAME
# signup-velocity reason as E2E_PLAIN_USER_EMAIL above -- its 4 scenarios need a real,
# already-logged-in-capable chat account, not a fresh `/auth/register` each time, and never
# share E2E_PLAIN_USER_EMAIL itself so a parallel admin-dashboard.spec.ts run (which
# enrols/step-ups MFA on that exact account) can never interleave with wakeup.spec.ts's own
# chat/login traffic on the one account.
E2E_WAKEUP_EMAIL="e2e-wakeup@example.test"
# T14.4.1-fix (block 14-D2-fix): plan-panel.spec.ts's own plain user, seeded directly for the
# SAME signup-velocity reason as E2E_PLAIN_USER_EMAIL/E2E_WAKEUP_EMAIL above. 14-D2 itself
# wired this spec through `registerAndReachChat` (a real `/auth/register` call) -- the 5th
# real UI registration sharing this step's one backend process (admin.spec.ts, mfa.spec.ts,
# auth.spec.ts's two inline registrations already used 4), which tipped `rag_app.risk.
# SignupTracker`'s already-documented 5-signups/hour/IP budget (see E2E_PLAIN_USER_EMAIL's
# own comment -- "a 6th registration in one run tripped it") and deterministically 403-ed
# whichever registration happened to land 6th in THIS run's own worker scheduling: auth.spec
# .ts's unrelated Mailpit-verify test (`auth.spec.ts:92:5`, reproduced 4/4 times, root-caused
# running `gate.sh --only e2e` and reading `test-results/.../error-context.md`: "signup
# blocked by risk check"). Never shares E2E_WAKEUP_EMAIL itself so a parallel wakeup*.spec.ts
# run's own chat/login traffic can never interleave with this one account's.
E2E_PLAN_PANEL_EMAIL="e2e-plan-panel@example.test"
# DEC-036 (DA-13-C-6): step_security-headers's own admin for its /admin walk -- separate from
# step_e2e's so the two steps' real MFA enrolments never collide in one gate DB lifetime.
E2E_SECHDR_ADMIN_EMAIL="e2e-sechdr-admin@example.test"
# Local secrets shared by every native backend these three steps start (git-ignored, 0600;
# same pattern as ROLES_ENV). MUST be identical across e2e/security-headers/zap-baseline
# within one gate DB lifetime: the master-key fingerprint (T11.2.4) is stored on the gate
# DB's FIRST ever backend start, and every later start against that SAME database refuses if
# its own DATA_MASTER_KEY does not match -- found running `--only
# e2e,security-headers,zap-baseline` for real (2026-10-17): three independently-random keys,
# one shared DB, two steps refused to start. Regenerated only when missing -- reused freely
# across separate gate.sh runs too (the gate DB itself is always recreated fresh, so an old
# key here is never a stale-data risk, only ever a throwaway local secret).
E2E_ENV="$GATE_STATE/e2e.env"
ensure_e2e_env() {
  [ -f "$E2E_ENV" ] && return 0
  mkdir -p "$GATE_STATE" || return 1
  chmod 700 "$GATE_STATE" || return 1
  (
    umask 077
    {
      echo "# Local secrets shared by the e2e/security-headers/zap-baseline native backends."
      echo "# Git-ignored; regenerated only when missing (see the comment above in gate.sh)."
      echo "E2E_JWT_SECRET=$(openssl rand -hex 24)"
      echo "E2E_CSRF_SECRET=$(openssl rand -hex 24)"
      echo "E2E_LOCKOUT_SECRET=$(openssl rand -hex 24)"
      echo "E2E_CLIENT_IP_KEY=$(openssl rand -hex 16)"
      echo "E2E_MASTER_KEY=$("$PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
      echo "E2E_ADMIN_PASSWORD=$(openssl rand -hex 24)"
    } >"$E2E_ENV.tmp" && mv -f "$E2E_ENV.tmp" "$E2E_ENV"
  )
}
# e2e_kill_port <port>: belt-and-suspenders (found the hard way: `npx next start` does not
# reliably exec-replace itself, so killing only $E2E_FRONTEND_PID once left an orphaned
# `next-server` still listening) -- whatever actually ended up bound to the port dies too,
# not just the PID this step itself backgrounded.
e2e_kill_port() {
  local pid
  pid="$(ss -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print $NF}' | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
  [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
}
e2e_cleanup() {
  [ -n "$E2E_FRONTEND_PID" ] && kill "$E2E_FRONTEND_PID" 2>/dev/null
  [ -n "$E2E_BACKEND_PID" ] && kill "$E2E_BACKEND_PID" 2>/dev/null
  [ -n "$E2E_FRONTEND_PID" ] && wait "$E2E_FRONTEND_PID" 2>/dev/null
  [ -n "$E2E_BACKEND_PID" ] && wait "$E2E_BACKEND_PID" 2>/dev/null
  e2e_kill_port "$E2E_BACKEND_PORT"
  e2e_kill_port "$E2E_FRONTEND_PORT"
}
# e2e_wait_healthy <url> <pid> <seconds> <log file>: polls <url>; fails fast if <pid> exited.
e2e_wait_healthy() {
  local url="$1" pid="$2" tries="$3" log="$4" _
  for _ in $(seq 1 "$tries"); do
    curl -fsS -m 2 "$url" >/dev/null 2>&1 && return 0
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  echo "  FAIL: not healthy after ${tries}s ($url); last log lines:"
  tail -n 20 "$log" | sed 's/^/  /'
  return 1
}
# e2e_ensure_mailpit: brings up the gate's own Mailpit (compose.gate.yml) -- idempotent,
# shared by e2e/security-headers/zap-baseline (all reuse e2e_start_backend). Not healthchecked
# (same as the dev stack's Mailpit): `up -d` returning means the container is running, not
# necessarily ready to accept SMTP connections yet -- send_verification_email's own
# best-effort retry-free send already tolerates a transient refused connection (T12.1.11).
e2e_ensure_mailpit() {
  compose up -d mailpit 2>&1 | sed 's/^/  /'
}
# e2e_start_backend <log dir>: sets E2E_BACKEND_PID + E2E_CLIENT_IP_KEY.
e2e_start_backend() {
  local log_dir="$1"
  ensure_e2e_env || return 1
  set -a
  # shellcheck source=/dev/null
  . "$E2E_ENV"
  set +a
  e2e_ensure_mailpit || return 1
  echo "  native backend on 127.0.0.1:$E2E_BACKEND_PORT (log: $log_dir/backend.log)"
  # T12.4.4/T12.4.5: SMTP points at the gate's own Mailpit (not "" any more) and
  # PUBLIC_APP_URL points at the e2e frontend -- register's verification email actually
  # sends and its link actually points at a page this run serves, so the dedicated Mailpit
  # e2e test (auth.spec.ts) can fetch and follow it.
  (cd "$REPO_ROOT/backend" && exec env \
      DATABASE_URL="$DATABASE_URL" ENV=dev JWT_SECRET="$E2E_JWT_SECRET" CSRF_SECRET="$E2E_CSRF_SECRET" \
      LOCKOUT_HMAC_SECRET="$E2E_LOCKOUT_SECRET" \
      DATA_MASTER_KEY="$E2E_MASTER_KEY" CLIENT_IP_HMAC_KEYS="$E2E_CLIENT_IP_KEY" \
      FRONTEND_ORIGIN="http://localhost:$E2E_FRONTEND_PORT" OLLAMA_HOST="$OLLAMA_HOST" \
      DAILY_ANSWER_CAP=1000 SMTP_HOST=127.0.0.1 SMTP_PORT="$GATE_MAILPIT_SMTP_PORT" \
      PUBLIC_APP_URL="http://localhost:$E2E_FRONTEND_PORT" \
      ADMIN_LIVE_MODE=sse \
      "$VENV/bin/uvicorn" rag_app.api.app:create_app --factory \
        --host 127.0.0.1 --port "$E2E_BACKEND_PORT" \
  ) >"$log_dir/backend.log" 2>&1 &
  E2E_BACKEND_PID=$!
  e2e_wait_healthy "http://127.0.0.1:$E2E_BACKEND_PORT/health" "$E2E_BACKEND_PID" 60 "$log_dir/backend.log"
}
# e2e_start_frontend <log dir>: needs E2E_CLIENT_IP_KEY already set by e2e_start_backend.
e2e_start_frontend() {
  local log_dir="$1"
  echo "  frontend build + next start on 127.0.0.1:$E2E_FRONTEND_PORT (log: $log_dir/frontend.log)"
  # The direct binary, not `npx next ...`: npx does not reliably exec-replace itself with
  # the resolved binary, which left an orphaned `next-server` running after $E2E_FRONTEND_PID
  # (npx's own PID) was killed -- found running this step for real (e2e_kill_port above is
  # the belt; this is the suspenders).
  (cd "$REPO_ROOT/frontend" \
     && BACKEND_URL="http://127.0.0.1:$E2E_BACKEND_PORT" CLIENT_IP_HMAC_KEYS="$E2E_CLIENT_IP_KEY" \
        CLIENT_IP_TRUSTED_HOPS=0 ./node_modules/.bin/next build \
     && exec env BACKEND_URL="http://127.0.0.1:$E2E_BACKEND_PORT" CLIENT_IP_HMAC_KEYS="$E2E_CLIENT_IP_KEY" \
        CLIENT_IP_TRUSTED_HOPS=0 ./node_modules/.bin/next start -p "$E2E_FRONTEND_PORT" -H 127.0.0.1 \
  ) >"$log_dir/frontend.log" 2>&1 &
  E2E_FRONTEND_PID=$!
  e2e_wait_healthy "http://127.0.0.1:$E2E_FRONTEND_PORT/api/health" "$E2E_FRONTEND_PID" 90 "$log_dir/frontend.log"
}
# e2e_start_stack <log dir>: both of the above, with the shared cleanup trap armed first.
e2e_start_stack() {
  mkdir -p "$1"
  trap e2e_cleanup EXIT
  e2e_start_backend "$1" || return 1
  e2e_start_frontend "$1" || return 1
}
# e2e_seed_admin: T13.2.10, step_e2e only (security-headers seeds its own admin, DEC-036;
# zap-baseline has no use for either account). Needs DATABASE_URL (exported by the gate's own DB setup, same
# value e2e_start_backend's native backend already uses) and E2E_ADMIN_PASSWORD (sourced
# from E2E_ENV by e2e_start_backend's own `. "$E2E_ENV"`, still exported in THIS shell once
# that function returns -- bash functions share the caller's environment unless run in a
# `( ... )` subshell, which ensure_e2e_env's write block is, but e2e_start_backend's `set -a`
# source is not). Seeds BOTH the admin and the plain-user erasure target (DA-13-C-1) --
# the same password for both, fine for a throwaway pair scoped to one gate run.
# T13.9.2 (DEBT-056): DATA_MASTER_KEY="$E2E_MASTER_KEY" -- a pre-existing gap, found running
# wakeup.spec.ts for real: without it, `create_verified_user`'s own `wrap_key` (which reads
# DATA_MASTER_KEY from the environment AT CALL TIME, rag_app.crypto) wraps the new user's
# data key with whatever DATA_MASTER_KEY this script's OWN shell happens to have (usually
# none), not the native backend's `$E2E_MASTER_KEY` (e2e_start_backend) -- so the FIRST
# real decrypt (e.g. this seeded account's first chat message) always 409s as "cannot be
# decrypted right now". Every account seeded here had simply never sent a chat message
# before (enrol/MFA/admin-panel/erasure-target checks don't decrypt anything) -- nothing
# wakeup.spec.ts's OWN BFF/wake-up code could have caused.
e2e_seed_admin() {
  (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    DATA_MASTER_KEY="$E2E_MASTER_KEY" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_ADMIN_EMAIL" --role admin) \
    && (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    DATA_MASTER_KEY="$E2E_MASTER_KEY" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_ADMIN2_EMAIL" --role admin) \
    && (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    DATA_MASTER_KEY="$E2E_MASTER_KEY" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_ERASE_TARGET_EMAIL" --role user) \
    && (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    DATA_MASTER_KEY="$E2E_MASTER_KEY" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_PLAIN_USER_EMAIL" --role user) \
    && (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    DATA_MASTER_KEY="$E2E_MASTER_KEY" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_WAKEUP_EMAIL" --role user) \
    && (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    DATA_MASTER_KEY="$E2E_MASTER_KEY" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_PLAN_PANEL_EMAIL" --role user)
}

step_e2e() {
  need_venv || return 1
  [ -d "$REPO_ROOT/frontend/node_modules/@playwright" ] \
    || { missing_tool "@playwright/test (npm ci, in frontend/)"; return; }
  command -v npx >/dev/null || { missing_tool "npx (node toolchain)"; return; }
  if ! "$VENV/bin/python" -c 'import uvicorn' 2>/dev/null; then
    missing_tool "uvicorn (pip install -r backend/requirements.txt)"
    return
  fi

  e2e_start_stack "$GATE_STATE/e2e" || return 1
  e2e_seed_admin || return 1

  # "localhost", not "127.0.0.1": Next.js's own middleware (next start, self-hosted)
  # constructs its redirect Location from `new URL(path, request.url)` using "localhost" as
  # the canonical host regardless of the Host header the browser actually sent (reproduced:
  # a 127.0.0.1-based run got redirected mid-test to a DIFFERENT origin, http://localhost:…,
  # and the next request's Origin header then failed FRONTEND_ORIGIN's exact-match check).
  # Visiting "localhost" from the start keeps the browser's origin consistent with every
  # middleware redirect; FRONTEND_ORIGIN above is set to match.
  # auth.spec.ts: the chat-flow walk (T12.1.10/DEBT-035). admin.spec.ts: T12.3.6 case (5)/(6)
  # (client-state tampering buys an ordinary user nothing on /admin) + T13.2.10 (the real
  # admin dashboard, MFA via e2e/totp.ts + e2e_seed_admin above). mfa.spec.ts: T12.3.5/
  # DEC-024 (the QR code actually renders from the real backend-issued provisioning URI).
  # wakeup.spec.ts (T13.9.2/DEBT-056): the wake-up UX against a `page.route()`-injected
  # slow/503 "backend" (chat + login), incl. the negative control -- no extra env needed.
  # wakeup-slow.spec.ts (T13.11.6/DEBT-060, 13-J2): the OTHER cold-start shape -- a request
  # HELD (never refused) by `page.route()`, proving the purely-visual pending timer shows
  # the same banner after ~3s with no Retry button, then the real (delayed, never faked)
  # answer arrives exactly once; plus its own negative control.
  # wakeup-pipeline.spec.ts (T13.12.6/T13.12.7, 13-J3b, DEBT-062): the real pipeline steps
  # INSIDE that same banner (chat only) -- same HOLD technique, asserting the step list's
  # initial "Backend starting" state and its fully-done state once the real answer streams;
  # plus its own negative control.
  # not-found.spec.ts (T13.10.3/DEBT-057): the custom 404 page -- real HTTP 404 status, single
  # "Back to login" link, no path echo, /api/... negative control, phone width, no extra env.
  # plan-panel.spec.ts (T14.4.1-3, block 14-D2): the chat UI's visible-plan panel, through a
  # real chat turn. Added to this explicit list because this step never runs the whole
  # `e2e/` directory -- a spec not named here simply never executes in `gate.sh --full`/CI
  # (found running this block's own first `--full` attempt: the spec was committed and in
  # `touches`, but never appeared in the 29-test run until added here). **Fixed (14-D2-fix):**
  # originally registered through the UI (`registerAndReachChat`) -- see E2E_PLAN_PANEL_EMAIL's
  # own comment above for why that tipped the signup-velocity budget and 403-ed an unrelated
  # test; now logs in with its own seeded account instead, same pattern as E2E_WAKEUP_EMAIL.
  # MAILPIT_API_URL (T12.4.4): auth.spec.ts's register -> Mailpit -> verify test reads the
  # verification email through the gate's own Mailpit (e2e_ensure_mailpit, above).
  (cd "$REPO_ROOT/frontend" && E2E_BASE_URL="http://localhost:$E2E_FRONTEND_PORT" \
    MAILPIT_API_URL="http://127.0.0.1:$GATE_MAILPIT_API_PORT" \
    E2E_ADMIN_EMAIL="$E2E_ADMIN_EMAIL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    E2E_ADMIN2_EMAIL="$E2E_ADMIN2_EMAIL" \
    E2E_ERASE_TARGET_EMAIL="$E2E_ERASE_TARGET_EMAIL" \
    E2E_PLAIN_USER_EMAIL="$E2E_PLAIN_USER_EMAIL" \
    E2E_WAKEUP_EMAIL="$E2E_WAKEUP_EMAIL" \
    E2E_PLAN_PANEL_EMAIL="$E2E_PLAN_PANEL_EMAIL" \
    npx playwright test e2e/auth.spec.ts e2e/admin.spec.ts e2e/admin-dashboard.spec.ts \
      e2e/mfa.spec.ts e2e/wakeup.spec.ts e2e/wakeup-slow.spec.ts e2e/wakeup-pipeline.spec.ts \
      e2e/not-found.spec.ts e2e/plan-panel.spec.ts)
}

# T12.4.9: register -> Mailpit API -> open the link -> verified, and the same shape for
# password reset -- a NATIVE backend only (no frontend/Playwright needed: both flows are
# exercised at the HTTP/JSON level, like `restart-check`, never through a browser), against
# the gate's own Mailpit (`e2e_ensure_mailpit`, called by `e2e_start_backend`). Cheaper than
# `e2e` (tagged `db`, not `seed` -- no RAG/Ollama call in either flow) despite sharing the
# same backend-bringup helper.
step_email-e2e() {
  need_venv || return 1
  if ! "$VENV/bin/python" -c 'import uvicorn' 2>/dev/null; then
    missing_tool "uvicorn (pip install -r backend/requirements.txt)"
    return
  fi
  mkdir -p "$GATE_STATE/email-e2e"
  trap e2e_cleanup EXIT
  e2e_start_backend "$GATE_STATE/email-e2e" || return 1
  (cd "$REPO_ROOT/backend" && "$VENV/bin/python" -m rag_app.devtools.email_e2e_check \
    --backend-url "http://127.0.0.1:$E2E_BACKEND_PORT" \
    --mailpit-url "http://127.0.0.1:$GATE_MAILPIT_API_PORT")
}

# T12.2.3/T12.2.5 (E3/E9): the Playwright page walk (frontend/e2e/security-headers.spec.ts)
# against the SAME native backend+frontend pair as `e2e`. --full only, never CI.
step_security-headers() {
  need_venv || return 1
  [ -d "$REPO_ROOT/frontend/node_modules/@playwright" ] \
    || { missing_tool "@playwright/test (npm ci, in frontend/)"; return; }
  command -v npx >/dev/null || { missing_tool "npx (node toolchain)"; return; }
  if ! "$VENV/bin/python" -c 'import uvicorn' 2>/dev/null; then
    missing_tool "uvicorn (pip install -r backend/requirements.txt)"
    return
  fi

  e2e_start_stack "$GATE_STATE/security-headers" || return 1
  # DEC-036: seed this step's own admin (same mechanism as e2e_seed_admin) for the /admin walk.
  (cd "$REPO_ROOT/backend" && DATABASE_URL="$DATABASE_URL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    "$VENV/bin/python" -m rag_app.devtools.e2e_seed_admin --email "$E2E_SECHDR_ADMIN_EMAIL" --role admin) \
    || return 1
  (cd "$REPO_ROOT/frontend" && E2E_BASE_URL="http://localhost:$E2E_FRONTEND_PORT" \
    E2E_SECHDR_ADMIN_EMAIL="$E2E_SECHDR_ADMIN_EMAIL" E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" \
    npx playwright test e2e/security-headers.spec.ts)
}

# T12.2.6 (E8): OWASP ZAP passive baseline scan against the SAME native backend+frontend pair
# as `e2e`/`security-headers`. The official image, pinned by digest (resolved read-only via
# `docker buildx imagetools inspect`, never a mutable tag). The ZAP container reaches the
# host's own loopback port via `host.docker.internal` -- Docker Desktop's WSL2 backend
# resolves this automatically to the host (confirmed with a throwaway `curlimages/curl`
# container, 2026-10-17); `--network host` does NOT work the same way there (also confirmed:
# a container on `--network host` could not reach a host loopback port at all). Both the
# target host and any extra `docker run` flags a different environment might need (e.g. a
# native Linux Docker install's `--add-host=host.docker.internal:host-gateway`) are
# configurable, never hard-coded to this one machine's setup.
ZAP_IMAGE="ghcr.io/zaproxy/zaproxy@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6"
ZAP_TARGET_HOST="${ZAP_TARGET_HOST:-host.docker.internal}"
ZAP_RULES_FILE="$REPO_ROOT/scripts/zap/zap-rules.tsv"
step_zap-baseline() {
  need_venv || return 1
  command -v docker >/dev/null || { missing_tool "docker"; return; }
  [ -f "$ZAP_RULES_FILE" ] || { echo "  FAIL: $ZAP_RULES_FILE missing"; return 1; }
  if ! "$VENV/bin/python" -c 'import uvicorn' 2>/dev/null; then
    missing_tool "uvicorn (pip install -r backend/requirements.txt)"
    return
  fi

  local zap_log="$GATE_STATE/zap-baseline"
  e2e_start_stack "$zap_log" || return 1

  local report="$zap_log/report.json" docker_extra=()
  rm -f "$report"
  # shellcheck disable=SC2206 # our own space-separated flag list (one-off host setups only)
  [ -n "${ZAP_DOCKER_EXTRA_ARGS:-}" ] && docker_extra=($ZAP_DOCKER_EXTRA_ARGS)
  echo "  ZAP baseline against http://$ZAP_TARGET_HOST:$E2E_FRONTEND_PORT/ (image $ZAP_IMAGE)"
  docker run --rm "${docker_extra[@]}" -v "$zap_log:/zap/wrk/:rw" "$ZAP_IMAGE" \
    zap-baseline.py -t "http://$ZAP_TARGET_HOST:$E2E_FRONTEND_PORT/" -J report.json -I \
    2>&1 | sed 's/^/  /'
  # zap-baseline.py's OWN exit code is informational only here (its `-c`/threshold mechanism
  # is not used): the real pass/fail judgment is this project's, against the real `risk`
  # field of every alert in the JSON report (rag_app.devtools.zap_report) -- any High-risk
  # alert fails unless its plugin id is listed, with a reason, in $ZAP_RULES_FILE.
  "$PY" -m rag_app.devtools.zap_report --report "$report" --rules "$ZAP_RULES_FILE"
}

# --- backend venv matching the pins (DA-B-4) --------------------------------------------

req_hash() { # req_hash <tree>: build recipe + the requirement files (torch pin included)
  {
    echo "recipe=$VENV_RECIPE"
    cat "$1/backend/requirements-torch.txt" "$1/backend/requirements.txt" \
      "$1/backend/requirements-dev.txt" 2>/dev/null
  } | sha256sum | cut -c1-16
}

venv_in_sync() { # venv_in_sync <venv dir>: installed == REPO_ROOT's pins
  [ -x "$1/bin/python" ] \
    && "$1/bin/python" -m rag_app.devtools.venv_sync --root "$REPO_ROOT" >/dev/null 2>&1
}

# build_venv <dir>: runs in its own process under timeout (--build-venv).
build_venv() {
  local dir="$1" py_version="3.12"
  command -v uv >/dev/null || { echo "FAIL: uv not found (needed to build the gate venv)"; return 1; }
  mkdir -p "$VENV_CACHE" || return 1
  exec 9>"$VENV_CACHE/.lock"
  flock 9
  [ -f "$dir/.secrag-complete" ] && return 0   # a concurrent run built it
  rm -rf "$dir"
  if [ -x "$MAIN_VENV/bin/python" ]; then
    # Same interpreter as the main venv; torch is pinned in requirements-torch.txt (DA-B2-3).
    py_version="$("$MAIN_VENV/bin/python" -c 'import platform; print(platform.python_version())')"
  fi
  uv venv --quiet --seed --python "$py_version" "$dir" || return 1
  uv pip install --quiet --python "$dir/bin/python" -r "$REPO_ROOT/backend/requirements-torch.txt" \
    || return 1
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
    VENV_ERROR="the main venv ($MAIN_VENV) does not match this tree's pins — fix with: $MAIN_VENV/bin/pip install -r backend/requirements-torch.txt, then -r backend/requirements-dev.txt"
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

# DEC-066 change 6 (F24-into-14): the deterministic eval (DEC-064, seed 42) is only
# byte-reproducible when the native `ollama serve` runs with OLLAMA_NUM_PARALLEL=1 --
# concurrent requests otherwise race inside Ollama's own runner. tools/ops/resume.sh
# already exports it before starting the server; this is a safety net for anyone who
# started `ollama serve` another way. Informational only (never fails the gate): a
# remote/other Ollama (GATE_OLLAMA_HOST pointing elsewhere) has no local process to
# inspect, and that is a legitimate, un-checkable setup, not a problem to report.
check_ollama_num_parallel() {
  local pid val
  # Test-only override (same convention as GATE_LOCK_TEST_SLEEP): lets a test point at its
  # own throwaway process deterministically instead of racing the real pgrep against
  # whatever "ollama serve" may already be running on the host. The real code path never
  # sets this.
  if [ -n "${GATE_OLLAMA_PID_FOR_TEST:-}" ]; then
    pid="$GATE_OLLAMA_PID_FOR_TEST"
  else
    pid="$(pgrep -f '[o]llama serve' 2>/dev/null | head -1)" || true
  fi
  [ -n "$pid" ] || return 0   # no native server here -- nothing to warn about
  [ -r "/proc/$pid/environ" ] || return 0
  val="$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | sed -n 's/^OLLAMA_NUM_PARALLEL=//p' | tail -1)"
  if [ "$val" != "1" ]; then
    echo "WARNING: the native 'ollama serve' (pid $pid) was started without" >&2
    echo "  OLLAMA_NUM_PARALLEL=1 (current value: '${val:-unset}') -- the dialogue eval" >&2
    echo "  (DEC-064 seed 42) is NOT guaranteed reproducible under concurrent requests." >&2
    echo "  Restart it with: OLLAMA_NUM_PARALLEL=1 ~/.local/bin/ollama serve" >&2
  fi
}

# "<model> <digest>" of the embedding model Ollama serves now (DA-B-9); empty on error.
embed_model_digest() {
  (cd "$REPO_ROOT/backend" && "$PY" -m rag_app.devtools.ollama_digest --host "$OLLAMA_HOST" 2>/dev/null)
}

db_alembic_head() {
  compose exec -T db psql -U "$GATE_DB_USER" -d "$GATE_DB_NAME" -tAc 'SELECT version_num FROM alembic_version'
}

# seed_dir_for_head <head>: this alembic head's own seed directory (F31). Never shared
# with any other head, so two branches/worktrees on different heads never collide.
seed_dir_for_head() { echo "$SEEDS_DIR/$1"; }

# resolve_seed_files <head>: sets SEED_FILE/SEED_META_FILE to whichever of (a) the
# per-head directory (seed_dir_for_head) or (b) the legacy top-level SEED/SEED_META (only
# when its own recorded alembic_head still matches, i.e. a tree that predates F31 whose
# head happens to equal the legacy seed's) has a real seed for this head. Clears both and
# returns 1 (SEED_ERROR already set, naming --make-seed) when neither does.
SEED_FILE=""
SEED_META_FILE=""
resolve_seed_files() {
  local head="$1" dir legacy_head
  dir="$(seed_dir_for_head "$head")"
  if [ -f "$dir/seed.dump" ] && [ -f "$dir/seed.meta" ]; then
    SEED_FILE="$dir/seed.dump"
    SEED_META_FILE="$dir/seed.meta"
    return 0
  fi
  if [ -f "$SEED" ] && [ -f "$SEED_META" ]; then
    legacy_head="$(sed -n 's/^alembic_head=//p' "$SEED_META" 2>/dev/null)"
    if [ "$legacy_head" = "$head" ]; then
      SEED_FILE="$SEED"
      SEED_META_FILE="$SEED_META"
      return 0
    fi
  fi
  SEED_FILE=""
  SEED_META_FILE=""
  if [ -f "$SEED_META" ]; then
    SEED_ERROR="no gate seed for alembic head $head (checked $dir/seed.dump; the legacy seed at $SEED is for head ${legacy_head:-?}) — run: scripts/gate.sh --make-seed"
  else
    SEED_ERROR="no gate seed for alembic head $head (checked $dir/seed.dump) — run: scripts/gate.sh --make-seed (needs Ollama)"
  fi
  return 1
}

# resolve_seed_files_cli <head>: test-only re-entrant entry point (like --run-step /
# --build-venv below) so backend/tests/test_gate_script.py can exercise resolve_seed_files'
# head-selection / legacy-fallback / mismatch-message logic directly, with plain files on
# disk -- no Docker, no DB, no lock, no log file.
resolve_seed_files_cli() {
  if resolve_seed_files "$1"; then
    echo "SEED_FILE=$SEED_FILE"
    echo "SEED_META_FILE=$SEED_META_FILE"
    return 0
  fi
  echo "SEED_ERROR=$SEED_ERROR"
  return 1
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
  # F31: this tree's OWN current alembic head picks the seed directory -- never the
  # single shared top-level file another branch/worktree may have just rebuilt.
  local current_head
  current_head="$(db_alembic_head)"
  resolve_seed_files "$current_head" || return 2   # SEED_ERROR already set
  # Staleness (DA-B-9): corpus and embedding model build must match the seed too (the
  # alembic head match is already guaranteed by resolve_seed_files's directory choice).
  local seeded_hash current_hash seeded_model current_model
  seeded_hash="$(sed -n 's/^corpus_sha256=//p' "$SEED_META_FILE" 2>/dev/null)"
  if [ -f "$CHUNKS" ]; then
    current_hash="$(sha256sum "$CHUNKS" | cut -d' ' -f1)"
    if [ "$seeded_hash" != "$current_hash" ]; then
      SEED_ERROR="the corpus changed since the seed was made — run: scripts/gate.sh --make-seed"
      return 2
    fi
  fi
  seeded_model="$(sed -n 's/^embed_model=//p' "$SEED_META_FILE" 2>/dev/null)"
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
  if ! compose exec -T db pg_restore -U "$GATE_DB_USER" -d "$GATE_DB_NAME" \
         --data-only --disable-triggers --no-owner <"$SEED_FILE"; then
    SEED_ERROR="seed restore failed"
    return 2
  fi
  echo "  seed restored: $(sed -n 's/^chunks=//p' "$SEED_META_FILE") chunks ($(sed -n 's/^created=//p' "$SEED_META_FILE"); $seeded_model; alembic $current_head)"
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
  # F31: writes ONLY this tree's own current head's directory -- never the shared
  # top-level legacy seed.dump/seed.meta another worktree/branch may be relying on.
  local chunks head seed_dir
  head="$(db_alembic_head)"
  seed_dir="$(seed_dir_for_head "$head")"
  mkdir -p "$seed_dir"
  chmod 700 "$GATE_STATE" "$SEEDS_DIR" "$seed_dir" 2>/dev/null
  compose exec -T db pg_dump -U "$GATE_DB_USER" -d "$GATE_DB_NAME" -Fc --data-only \
    -t documents -t chunks >"$seed_dir/seed.dump.tmp" \
    || { echo "  FAIL: pg_dump"; rm -f "$seed_dir/seed.dump.tmp"; return 1; }
  chunks="$(compose exec -T db psql -U "$GATE_DB_USER" -d "$GATE_DB_NAME" -tAc 'SELECT count(*) FROM chunks')"
  mv -f "$seed_dir/seed.dump.tmp" "$seed_dir/seed.dump"
  {
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "corpus_sha256=$(sha256sum "$CHUNKS" | cut -d' ' -f1)"
    echo "embed_model=$model"
    echo "alembic_head=$head"
    echo "chunks=$chunks"
  } >"$seed_dir/seed.meta"
  echo "seed written: $seed_dir/seed.dump ($chunks chunks, $model, alembic $head) in $((SECONDS - t0))s"
}

# --- global gate lock (T13.14.4, Paralelo-B problem #2) --------------------------------

# acquire_gate_lock <mode>: blocks on GATE_LOCK_FILE (printing a clear wait message that
# names the current holder) for up to GATE_LOCK_TIMEOUT seconds, then FAILs cleanly.
# Holder info (block/label, branch, pid, mode, start time) is written to
# GATE_LOCK_HOLDER_FILE right after the lock is taken, so the next waiter can report who
# holds it. The lock is held on GATE_LOCK_FD, an fd opened in THIS process (via `exec
# {var}>file`): `flock` the external command, run as a child, locks that same open file
# description, but the lock survives the child's exit because this process's own copy of
# the fd stays open -- the classic bash `exec N>file; flock N` idiom. It is released the
# moment every fd referencing it closes, i.e. on normal exit or `kill -9` of this process;
# no cleanup trap is needed. Caveat: an fd opened this way is inherited (not close-on-exec)
# by every child this process forks (e.g. a `run_step`'s own `bash "$SELF" --run-step`), so
# a `kill -9` of ONLY the top gate.sh pid while such a child is still alive leaves the lock
# held until that child also exits -- killing the whole tree/process group (`pkill -9 -f
# "gate.sh"`, or a negative-pid `kill`) releases it immediately regardless, which is the
# normal way an agent stops a hung gate run.
acquire_gate_lock() {
  local mode="${1:-fast}" holder
  mkdir -p "$(dirname "$GATE_LOCK_FILE")" 2>/dev/null
  exec {GATE_LOCK_FD}>"$GATE_LOCK_FILE" \
    || { echo "FAIL: cannot open gate lock file $GATE_LOCK_FILE" >&2; return 1; }
  if ! flock -n "$GATE_LOCK_FD"; then
    holder="$(cat "$GATE_LOCK_HOLDER_FILE" 2>/dev/null)"
    echo "gate: waiting for the gate lock held by ${holder:-another run} (timeout ${GATE_LOCK_TIMEOUT}s; set GATE_LOCK_TIMEOUT to change)" >&2
    if ! flock -w "$GATE_LOCK_TIMEOUT" "$GATE_LOCK_FD"; then
      echo "FAIL: timed out after ${GATE_LOCK_TIMEOUT}s waiting for the gate lock held by ${holder:-another run}" >&2
      return 1
    fi
  fi
  printf 'block=%s branch=%s pid=%s mode=%s start=%s\n' \
    "${GATE_LOCK_LABEL:-$(basename "$REPO_ROOT")}" \
    "$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)" \
    "$$" "$mode" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$GATE_LOCK_HOLDER_FILE" 2>/dev/null
  [ "$GATE_LOCK_TEST_SLEEP" -gt 0 ] 2>/dev/null && sleep "$GATE_LOCK_TEST_SLEEP"
  return 0
}

# --- secrag/gate-full commit status (D-2026-09-27-7 b) ---------------------------------

# publish_gate_status <mode> <fail> <seconds>: every gate run ends here; only a --full PASS
# on a clean, unmoved tree posts `secrag/gate-full` = success for exactly the checked SHA
# (CD refuses a SHA without it). The decision and the detached publisher for the pre-push
# case live in scripts/cd/gate_publish.sh (tested, DA-C-4). Never changes the gate result.
publish_gate_status() {
  local mode="$1" fail="$2" total="$3" result=FAIL
  [ "$fail" -eq 0 ] && result=PASS
  # T13.14.4: release the global gate lock BEFORE forking gate_publish.sh. Every
  # lock-protected resource (the gate compose project/ports, native Ollama) is already done
  # with by this point; gate_publish.sh can itself `setsid nohup … &` a detached publisher
  # that waits up to 15 min for the SHA to reach GitHub, and an fd opened via `exec {var}>`
  # is inherited (not close-on-exec) by every process forked after it -- closing it here is
  # what stops that detached, long-lived watcher from silently holding the lock for the rest
  # of its wait and blocking every other gate.sh/with-lock.sh run for nothing (found by the
  # T13.14.6 trial: a concurrent pre-push queued behind a `--full` run that had already
  # exited, because its detached publisher was still alive and had inherited the fd).
  if [ -n "${GATE_LOCK_FD:-}" ]; then
    exec {GATE_LOCK_FD}>&- 2>/dev/null || true
    unset GATE_LOCK_FD
  fi
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

usage() { sed -n '2,32p' "$SELF" | sed 's/^# \{0,1\}//'; }

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
  check_ollama_num_parallel
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

# --- gate log (C8c, T11c.E1) -------------------------------------------------------------

# Re-entrant self-invocations (a step, the cached-venv builder, the F31 seed-file
# resolver) and purely informational flags never open their own log file: their output
# already lands in the top-level run's log (steps) or there is no run to record
# (--list/-h/--help).
case "${1:-}" in
  --run-step | --build-venv | --list | -h | --help)
    main "$@"
    exit $?
    ;;
  --resolve-seed-files)
    shift
    resolve_seed_files_cli "$1"
    exit $?
    ;;
esac

# Every real run (fast/full/only/seed, incl. the pre-push path) takes the global gate lock
# first (T13.14.4) -- re-entrant self-invocations and informational flags above never do.
acquire_gate_lock "${1:-fast}" || exit 1

GATE_LOG_DIR="${GATE_LOG_DIR:-$HOME/secrag-gate-logs}"
# DA-E-2: the directory and every log file are private (0700 / 0600) -- a local umask for
# just these two creations, never changing the umask `main "$@"` itself runs under. A
# pre-existing, looser-permissioned directory/file (e.g. from before this check existed) is
# tightened defensively on every run too.
(umask 077 && mkdir -p "$GATE_LOG_DIR") 2>/dev/null
chmod 700 "$GATE_LOG_DIR" 2>/dev/null || true
if [ -w "$GATE_LOG_DIR" ]; then
  GATE_LOG_FILE="$GATE_LOG_DIR/gate-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
  (umask 077 && : >"$GATE_LOG_FILE") 2>/dev/null
  chmod 600 "$GATE_LOG_FILE" 2>/dev/null || true
  main "$@" 2>&1 | tee "$GATE_LOG_FILE"
  gate_rc="${PIPESTATUS[0]}"
  echo "gate log: $GATE_LOG_FILE" >&2
  exit "$gate_rc"
fi
echo "gate log: $GATE_LOG_DIR is not writable -- continuing without a log file" >&2
main "$@"
