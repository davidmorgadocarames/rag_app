#!/usr/bin/env bash
# Prerequisites check for Phase 11+ (T11.0.P). Prints one line per item:
#   OK       ready
#   KO       missing or broken locally — fix it (most: scripts/prereqs/install.sh)
#   PENDING  needs the user or the orchestrator (login, repo/Azure setting, decision)
# Exit code: 0 = every item OK, 1 = at least one KO, 2 = no KO but something PENDING.
#
# Read-only: it never logs in, never changes GitHub or Azure settings. GitHub queries use
# the Linux `gh` when it is logged in, otherwise the Windows `gh.exe` through interop.
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
  printf '%-8s %-44s %s\n' "$1" "$2" "$3"
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

if command -v shellcheck >/dev/null; then
  report OK "shellcheck" "$(shellcheck --version | awk '/^version:/ {print $2}')"
else
  report KO "shellcheck" "missing — run scripts/prereqs/install.sh shellcheck"
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
    "") report PENDING "GHCR visibility of $JOBS_PACKAGE" "decision (user): public like the other images, or registry credentials for the Jobs; package not pushed yet" ;;
    *) report PENDING "GHCR visibility of $JOBS_PACKAGE" "$vis — the Jobs need registry credentials, or make it public (user)" ;;
  esac
else
  for item in "repo variables for Job names" "Dependabot security updates (row 2)" "GHCR visibility of $JOBS_PACKAGE"; do
    report PENDING "$item" "no logged-in gh to query GitHub"
  done
fi

# The deploy job declares `deployments: write` (T10.5.7); the repository default
# token permissions only matter for jobs without an explicit `permissions:` block.
if awk '/^  deploy:/{d=1} d && /deployments: write/{f=1} END{exit !f}' .github/workflows/cd.yml 2>/dev/null; then
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

AZL=""
for d in $AZ_LINUX_DIR_GLOB; do
  [ -x "$d/bin/az" ] && AZL="$d/bin/az"
done
if [ -n "$AZL" ]; then
  report OK "Linux Azure CLI (user venv)" "$("$AZL" version --query '"azure-cli"' -o tsv 2>/dev/null) at $AZL"
  if [ "$(readlink -f "$(command -v az)")" = "$(readlink -f "$AZL")" ]; then
    report OK "Linux Azure CLI is the default az" "$(command -v az)"
  else
    report PENDING "Linux Azure CLI is the default az" "orchestrator: scripts/prereqs/install.sh --link-az (now: az-linux)"
  fi
  if "$AZL" account show --output none >/dev/null 2>&1; then
    report OK "Linux Azure CLI logged in" "az account show"
    # Data-plane read access to the backups (shared-key access will be disabled).
    oid="$("$AZL" ad signed-in-user show --query id -o tsv 2>/dev/null)"
    n="$("$AZL" role assignment list --assignee "${oid:-none}" --all \
          --query "length([?roleDefinitionName=='Storage Blob Data Reader'])" -o tsv 2>/dev/null)"
    if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
      report OK "Storage Blob Data Reader for the user" "$n assignment(s)"
    else
      report PENDING "Storage Blob Data Reader for the user" "orchestrator (Azure write, row 40, after the Storage Account exists)"
    fi
  else
    report PENDING "Linux Azure CLI logged in" "user: run 'az-linux login'"
    report PENDING "Storage Blob Data Reader for the user" "needs a logged-in Linux Azure CLI to check"
  fi
else
  report KO "Linux Azure CLI (user venv)" "missing — run scripts/prereqs/install.sh az"
fi

echo ""
echo "Summary: $n_ko KO, $n_pending PENDING"
if [ "$n_ko" -gt 0 ]; then exit 1; fi
if [ "$n_pending" -gt 0 ]; then exit 2; fi
exit 0
