#!/usr/bin/env bash
# Phase gate: enforce the project's Definition of Done (mechanical norms).
# Run before declaring a phase done / before pushing. Non-zero exit == blocked.
# See docs/DEFINITION_OF_DONE.md for the full list of norms.
set -uo pipefail

# Prefer the locally-installed Node/Python toolchains (WSL2, no sudo) over any
# Windows tools leaking in via WSL PATH interop.
export PATH="$HOME/.local/bin:$PATH"
hash -r 2>/dev/null || true

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/backend/.venv"
PY="$VENV/bin/python"
RUFF="$VENV/bin/ruff"
MYPY="$VENV/bin/mypy"
PYTEST="$VENV/bin/pytest"
PRECOMMIT="$VENV/bin/pre-commit"

fail=0
note_fail() {
  echo "  FAIL: $1"
  fail=1
}
step() { echo ""; echo "== $1 =="; }

# 1. Environment: must be WSL2 / Linux
step "1. environment (WSL2)"
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "  PASS (WSL2)"
else
  note_fail "not running under WSL2 (project norm #1). See docs/DEFINITION_OF_DONE.md"
fi

# 2. No committed .env
step "2. secrets: .env not tracked"
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  note_fail ".env is tracked by git"
else
  echo "  PASS"
fi

# 3. Dependencies pinned (every requirement line has ==; skip -r/options/comments)
step "3. dependencies pinned"
unpinned=""
while IFS= read -r line; do
  case "$line" in
    '' | '#'* | -*) continue ;;
  esac
  pkg="${line%%#*}"
  pkg="$(echo "$pkg" | tr -d '[:space:]')"
  [ -z "$pkg" ] && continue
  case "$pkg" in
    *==*) : ;;
    *) unpinned="$unpinned $pkg" ;;
  esac
done < <(cat backend/requirements*.txt 2>/dev/null)
if [ -n "$unpinned" ]; then
  note_fail "unpinned dependencies:$unpinned"
else
  echo "  PASS"
fi

# 4. venv present
step "4. backend venv present"
if [ -x "$PY" ]; then
  echo "  PASS ($($PY --version 2>&1))"
else
  note_fail "backend/.venv missing — create it and install requirements-dev.txt"
fi

# 5-9. backend quality (only if venv exists)
if [ -x "$PY" ]; then
  step "5. ruff (lint)"
  (cd backend && "$RUFF" check .) && echo "  PASS" || note_fail "ruff check"

  step "6. ruff (format)"
  (cd backend && "$RUFF" format --check .) && echo "  PASS" || note_fail "ruff format"

  step "7. mypy (strict)"
  (cd backend && "$MYPY" src) && echo "  PASS" || note_fail "mypy"

  step "8. pytest"
  (cd backend && "$PYTEST" -q) && echo "  PASS" || note_fail "pytest"

  step "9. gitleaks (secret scan)"
  if [ -x "$PRECOMMIT" ]; then
    "$PRECOMMIT" run gitleaks --all-files && echo "  PASS" || note_fail "gitleaks"
  else
    echo "  SKIP (pre-commit not installed in venv)"
  fi
fi

# 10. frontend (only if dependencies are installed)
step "10. frontend (lint / typecheck / build)"
if [ -d frontend/node_modules ]; then
  (cd frontend && npm run lint && npm run typecheck && npm run build) \
    && echo "  PASS" || note_fail "frontend checks"
else
  echo "  SKIP (frontend/node_modules missing)"
fi

# 11. eval gate (Phase 4+)
step "11. eval gate (Phase 4+)"
if [ -f backend/src/rag_app/eval/gate.py ]; then
  (cd backend && PYTHONPATH=src "$PY" -m rag_app.eval.gate) \
    && echo "  PASS" || note_fail "eval gate"
else
  echo "  SKIP (no eval gate yet)"
fi

echo ""
echo "============================================"
if [ "$fail" -eq 0 ]; then
  echo "GATE: PASS"
else
  echo "GATE: FAIL — see failures above (docs/DEFINITION_OF_DONE.md)"
fi
echo "============================================"
exit "$fail"
