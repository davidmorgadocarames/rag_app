#!/usr/bin/env bash
# Prerequisites check for Phase 11+ (T11.0.P). Prints one line per item:
#   OK       ready
#   KO       missing or broken locally — fix it (most: scripts/prereqs/install.sh)
#   PENDING  needs the user or the orchestrator (login, repo/Azure setting, decision)
# Exit code: 0 = every item OK, 1 = at least one KO, 2 = no KO but something PENDING.
#
# Read-only: it never logs in, never changes GitHub or Azure settings. GitHub queries use
# the Linux `gh` when it is logged in, otherwise the Windows `gh.exe` through interop.
# The Linux Azure CLI is only run through the ~/.local/bin/az-linux wrapper (own config
# dir, never a Windows profile: not under /mnt and not on any 9p/drvfs mount).
#
# Optional input: BACKUP_STORAGE_SCOPE=<resource id of the backup Storage Account>
# (exists from row 40) scopes the Storage Blob Data Reader check; unset → PENDING.
set -uo pipefail

export PATH="$HOME/.local/bin:$PATH"
hash -r 2>/dev/null || true

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

# Repo variables that will hold the Azure Job names (consumed by CD in row 24).
JOB_VARS=(AZURE_MIGRATE_JOB AZURE_PURGE_JOB AZURE_BACKUP_JOB)
JOBS_PACKAGE="rag_app-jobs"
AZ_LINUX_DIR_GLOB="$HOME/.local/opt/azure-cli-*"

n_ko=0
n_pending=0
report() { # report <OK|KO|PENDING> <item> <detail>
  printf '%-8s %-46s %s\n' "$1" "$2" "$3"
  case "$1" in
    KO) n_ko=$((n_ko + 1)) ;;
    PENDING) n_pending=$((n_pending + 1)) ;;
  esac
}

# version_ge <have> <want>: dotted numeric comparison.
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# --- Local toolchain ---------------------------------------------------------------

if grep -qi microsoft /proc/version 2>/dev/null; then
  report OK "environment: WSL2" "$(uname -r)"
else
  report KO "environment: WSL2" "not running under WSL2"
fi

if command -v node >/dev/null; then
  v="$(node --version | tr -d v)"
  if version_ge "$v" 22.13.0 && [ "${v%%.*}" = 22 ]; then
    report OK "node >= 22.13 (22.x)" "$v at $(command -v node)"
  else
    report KO "node >= 22.13 (22.x)" "$v — run scripts/prereqs/install.sh node"
  fi
else
  report KO "node >= 22.13 (22.x)" "missing — run scripts/prereqs/install.sh node"
fi

if command -v npm >/dev/null; then
  v="$(npm --version 2>/dev/null)"
  if version_ge "$v" 11.0.0; then
    report OK "npm >= 11" "$v"
  else
    report KO "npm >= 11" "$v — run scripts/prereqs/install.sh node"
  fi
else
  report KO "npm >= 11" "missing"
fi

docker_ok=0
if docker compose version --short >/dev/null 2>&1; then
  docker_ok=1
fi

for tool in psql pg_dump; do
  if command -v "$tool" >/dev/null && "$tool" --version | grep -q ' 16\.'; then
    report OK "$tool 16" "$("$tool" --version)"
  elif [ "$docker_ok" = 1 ]; then
    report OK "$tool 16" "docker fallback: docker run --rm pgvector/pgvector:pg16 $tool …"
  else
    report KO "$tool 16" "missing — run scripts/prereqs/install.sh pg (or use the docker fallback)"
  fi
done

if command -v age >/dev/null && command -v age-keygen >/dev/null; then
  report OK "age + age-keygen" "$(age --version)"
else
  report KO "age + age-keygen" "missing — run scripts/prereqs/install.sh age"
fi

# uv installs the pinned CPython for the Azure CLI venv and enforces the lock's hashes
# (DA-A2-3); keep UV_MIN_VERSION in sync with install.sh.
UV_MIN_VERSION="0.12.17"
if command -v uv >/dev/null; then
  v="$(uv --version | awk '{print $2}')"
  if version_ge "$v" "$UV_MIN_VERSION"; then
    report OK "uv >= $UV_MIN_VERSION" "$v at $(command -v uv)"
  else
    report KO "uv >= $UV_MIN_VERSION" "$v — run: uv self update"
  fi
else
  report KO "uv >= $UV_MIN_VERSION" "missing — install from https://docs.astral.sh/uv/ into ~/.local/bin"
fi

if command -v shellcheck >/dev/null; then
  report OK "shellcheck" "$(shellcheck --version | awk '/^version:/ {print $2}')"
else
  report KO "shellcheck" "missing — run scripts/prereqs/install.sh shellcheck"
fi

if command -v jq >/dev/null; then
  report OK "jq" "$(jq --version)"
else
  report KO "jq" "missing — run scripts/prereqs/install.sh jq"
fi

if [ "$docker_ok" = 1 ]; then
  v="$(docker compose version --short 2>/dev/null | tr -d v)"
  if version_ge "$v" 2.20.0; then
    report OK "docker compose >= 2.20" "$v"
  else
    report KO "docker compose >= 2.20" "$v (needs 2.20 for depends_on required: false)"
  fi
else
  report KO "docker compose >= 2.20" "docker not reachable from WSL — enable Docker Desktop's WSL integration (user)"
fi

# --- Pre-push gate (DA-B-1) ------------------------------------------------------------
# Git runs .githooks/pre-push from the working tree and silently ignores it when
# core.hooksPath does not point there or the file lost its exec bit (UNC writes do that).

hooks_path="$(git config --get core.hooksPath 2>/dev/null)"
if [ "$hooks_path" = .githooks ]; then
  report OK "pre-push gate active (core.hooksPath)" ".githooks"
else
  report KO "pre-push gate active (core.hooksPath)" "'${hooks_path:-unset}' — run: git config core.hooksPath .githooks"
fi
if [ -x .githooks/pre-push ]; then
  report OK "pre-push hook executable on disk" ".githooks/pre-push"
else
  report KO "pre-push hook executable on disk" "run: chmod +x .githooks/pre-push (git ignores it silently)"
fi
not_exec="$(git ls-files -s -- '*.sh' '.githooks/*' | awk '$1 != "100755" {printf " %s", $4}')"
if [ -z "$not_exec" ]; then
  report OK "tracked scripts 100755 in git" "*.sh, .githooks/*"
else
  report KO "tracked scripts 100755 in git" "git update-index --chmod=+x$not_exec"
fi

# --- GitHub CLI ----------------------------------------------------------------------

GH=""
if command -v gh >/dev/null; then
  report OK "gh (Linux)" "$(gh --version | head -1)"
  if gh auth status >/dev/null 2>&1; then
    report OK "gh (Linux) logged in" "gh auth status"
    GH="gh"
  else
    report PENDING "gh (Linux) logged in" "user: run 'gh auth login' in WSL"
  fi
else
  report KO "gh (Linux)" "missing — run scripts/prereqs/install.sh gh"
fi
if [ -z "$GH" ] && command -v gh.exe >/dev/null && gh.exe auth status >/dev/null 2>&1; then
  GH="gh.exe"
fi

REPO_SLUG="$(git remote get-url origin 2>/dev/null | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')"

if [ -n "$GH" ] && [ -n "$REPO_SLUG" ]; then
  vars="$("$GH" variable list -R "$REPO_SLUG" 2>/dev/null | awk '{print $1}')"
  missing=""
  for v in "${JOB_VARS[@]}"; do
    grep -qx "$v" <<<"$vars" || missing="$missing $v"
  done
  if [ -z "$missing" ]; then
    report OK "repo variables for Job names" "${JOB_VARS[*]}"
  else
    report PENDING "repo variables for Job names" "orchestrator (row 40): missing$missing"
  fi

  sec="$("$GH" api "repos/$REPO_SLUG/automated-security-fixes" --jq .enabled 2>/dev/null)" || sec=""
  if [ "$sec" = "true" ]; then
    report OK "Dependabot security updates (row 2)" "enabled"
  else
    report PENDING "Dependabot security updates (row 2)" "user: Settings → Code security → Dependabot security updates (${sec:-unknown})"
  fi

  owner="${REPO_SLUG%%/*}"
  # gh prints the error body on stdout for a 404, so rely on the exit code.
  vis="$("$GH" api "users/$owner/packages/container/$JOBS_PACKAGE" --jq .visibility 2>/dev/null)" || vis=""
  case "$vis" in
    public) report OK "GHCR visibility of $JOBS_PACKAGE" "public" ;;
    "") report PENDING "GHCR visibility of $JOBS_PACKAGE" "orchestrator (row 40): package not pushed yet; set public when first pushed (D-2026-09-27-1)" ;;
    *) report PENDING "GHCR visibility of $JOBS_PACKAGE" "$vis — the Jobs need registry credentials, or make it public (user)" ;;
  esac
else
  for item in "repo variables for Job names" "Dependabot security updates (row 2)" "GHCR visibility of $JOBS_PACKAGE"; do
    report PENDING "$item" "no logged-in gh to query GitHub"
  done
fi

# The deploy job declares `deployments: write` (T10.5.7); the repository default
# token permissions only matter for jobs without an explicit `permissions:` block.
# Only lines inside the `deploy:` job block count (it ends at the next job key or a
# top-level key).
if awk '
  /^[^[:space:]#]/ { d = 0 }
  /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ { d = ($1 == "deploy:") }
  d && /^[[:space:]]+deployments:[[:space:]]*write([[:space:]]|#|$)/ { f = 1 }
  END { exit !f }' .github/workflows/cd.yml 2>/dev/null; then
  detail="declared on the deploy job in cd.yml"
  if [ -n "$GH" ] && [ -n "$REPO_SLUG" ]; then
    def="$("$GH" api "repos/$REPO_SLUG/actions/permissions/workflow" --jq .default_workflow_permissions 2>/dev/null)"
    detail="$detail; repo default token: ${def:-unknown}"
  fi
  report OK "workflow permission deployments: write" "$detail"
else
  report KO "workflow permission deployments: write" "not declared on the deploy job in cd.yml"
fi

# --- Azure CLI -----------------------------------------------------------------------

# Windows `az` through interop: must be callable from a shell trap on Ctrl-C.
WINAZ=""
IFS=: read -ra path_dirs <<<"$PATH"
for d in "${path_dirs[@]}"; do
  case "$d" in /mnt/*) [ -x "$d/az" ] && { WINAZ="$d/az"; break; } ;; esac
done
if [ -n "$WINAZ" ]; then
  marker="$(mktemp)"
  # shellcheck disable=SC2016  # expanded by the inner bash
  timeout -s INT 3 bash -c 'trap '\''"$1" version --output none >/dev/null 2>&1 && echo ran > "$2"; exit 130'\'' INT; sleep 60 & wait' \
    _ "$WINAZ" "$marker" >/dev/null 2>&1
  if grep -q ran "$marker"; then
    report OK "az (Windows interop), trap on SIGINT" "$WINAZ"
  else
    report KO "az (Windows interop), trap on SIGINT" "the trap did not run az"
  fi
  rm -f "$marker"
else
  report KO "az (Windows interop), trap on SIGINT" "no Windows az on PATH"
fi

# The Linux CLI is only ever run through the wrapper that install.sh generates: it
# pins AZURE_CONFIG_DIR to ~/.azure-linux, so nothing here reads or writes the Windows
# profile that ~/.azure points to. Never call the venv's az directly.
AZL_DIR=""
for d in $AZ_LINUX_DIR_GLOB; do
  [ -x "$d/bin/az" ] && [ -f "$d/.secrag-install-complete" ] && AZL_DIR="$d"
done
AZW="$HOME/.local/bin/az-linux"
is_wrapper() { [ -f "$1" ] && grep -q SECRAG_AZ_WRAPPER "$1" 2>/dev/null; }

# az_config_dir <entry point>: the config dir that entry point would use.
az_config_dir() {
  if is_wrapper "$1"; then
    readlink -m "${AZURE_CONFIG_DIR:-$HOME/.azure-linux}"
  else
    readlink -m "${AZURE_CONFIG_DIR:-$HOME/.azure}"
  fi
}

if [ -n "$AZL_DIR" ]; then
  v="$("$AZL_DIR/bin/python" -c 'import importlib.metadata as m; print(m.version("azure-cli"))' 2>/dev/null)"
  report OK "Linux Azure CLI (user venv, hashed lock)" "${v:-?} at $AZL_DIR"
else
  report KO "Linux Azure CLI (user venv, hashed lock)" "missing or incomplete — run scripts/prereqs/install.sh az"
fi

az_safe=0
# windows_fs <path>: prints the reason when <path> is on a Windows drive — under /mnt, or
# on a 9p/drvfs filesystem wherever it is mounted (DA-A2-1); the nearest existing
# ancestor decides when the directory does not exist yet.
windows_fs() {
  case "$1" in /mnt/*) echo "under /mnt"; return 0 ;; esac
  local p="$1" fs
  while [ ! -e "$p" ]; do p="$(dirname "$p")"; done
  fs="$(stat -f -c %T "$p" 2>/dev/null) $(findmnt -T "$p" -no FSTYPE 2>/dev/null)"
  case " $fs " in
    *" v9fs "* | *" 9p "* | *" drvfs "*) echo "on a Windows drive ($fs at $p)"; return 0 ;;
  esac
  return 1
}

CFG_ITEM="Linux Azure CLI config dir not on a Windows drive"
if [ -n "$AZL_DIR" ] && is_wrapper "$AZW"; then
  cfg="$(az_config_dir "$AZW")"
  if why="$(windows_fs "$cfg")"; then
    report KO "$CFG_ITEM" "$cfg is $why — unset AZURE_CONFIG_DIR / fix ~/.azure-linux"
  else
    report OK "$CFG_ITEM" "$cfg"
    az_safe=1
  fi
elif [ -n "$AZL_DIR" ]; then
  report KO "$CFG_ITEM" "az-linux is not the config-dir wrapper (would use $(az_config_dir "$AZW")) — run scripts/prereqs/install.sh az"
fi

AZ_DEFAULT="$(command -v az 2>/dev/null)"
if is_wrapper "$AZ_DEFAULT"; then
  report OK "Linux Azure CLI is the default az" "$AZ_DEFAULT (wrapper)"
elif [ -n "$AZ_DEFAULT" ] && [[ "$(readlink -f "$AZ_DEFAULT")" == "$HOME"/.local/opt/azure-cli-*/bin/az ]]; then
  report KO "Linux Azure CLI is the default az" "$AZ_DEFAULT links to the venv without the wrapper (uses ~/.azure) — run scripts/prereqs/install.sh --link-az"
else
  report PENDING "Linux Azure CLI is the default az" "orchestrator: scripts/prereqs/install.sh --link-az (now: ${AZ_DEFAULT:-none})"
fi

# Real token check (the profile alone is not a login): output discarded, only the
# status is printed. Runs only when the wrapper keeps the config dir off /mnt.
az_logged_in=0
if [ "$az_safe" = 1 ]; then
  if "$AZW" account get-access-token --output none >/dev/null 2>&1; then
    report OK "Linux Azure CLI logged in" "access token obtained (not shown)"
    az_logged_in=1
  else
    report PENDING "Linux Azure CLI logged in" "user: az login in WSL (browser flow: BROWSER=explorer.exe az login --tenant <tenant>; device code is blocked by the tenant's security defaults)"
  fi
else
  report PENDING "Linux Azure CLI logged in" "not checked: fix the Linux CLI wrapper / config dir first"
fi

# Data-plane read access to the backups (shared-key access will be disabled), scoped
# to the backup Storage Account: BACKUP_STORAGE_SCOPE=<its resource id> (row 40).
BACKUP_STORAGE_SCOPE="${BACKUP_STORAGE_SCOPE:-}"
if [ -z "$BACKUP_STORAGE_SCOPE" ]; then
  report PENDING "Storage Blob Data Reader on the backup account" "orchestrator (row 40): create the account + role, then rerun with BACKUP_STORAGE_SCOPE=<account resource id>"
elif [ "$az_logged_in" = 1 ]; then
  oid="$("$AZW" ad signed-in-user show --query id -o tsv 2>/dev/null)"
  n="$("$AZW" role assignment list --assignee "${oid:-none}" --scope "$BACKUP_STORAGE_SCOPE" \
        --include-inherited --role "Storage Blob Data Reader" --query "length(@)" -o tsv 2>/dev/null)"
  if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
    report OK "Storage Blob Data Reader on the backup account" "$n assignment(s) at or above ${BACKUP_STORAGE_SCOPE##*/}"
  else
    report PENDING "Storage Blob Data Reader on the backup account" "orchestrator (Azure write, row 40): none at ${BACKUP_STORAGE_SCOPE##*/}"
  fi
else
  report PENDING "Storage Blob Data Reader on the backup account" "needs a logged-in Linux Azure CLI to check"
fi

echo ""
echo "Summary: $n_ko KO, $n_pending PENDING"
if [ "$n_ko" -gt 0 ]; then exit 1; fi
if [ "$n_pending" -gt 0 ]; then exit 2; fi
exit 0
