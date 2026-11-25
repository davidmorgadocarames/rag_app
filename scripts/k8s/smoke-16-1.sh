#!/usr/bin/env bash
# Static manifest tests for Phase 16 (T16.1.1-T16.1.7). No cluster needed: kustomize build of
# both overlays, kubeconform + kube-linter on each (kube-linter is now ENFORCED, T16.1.7 --
# this is the gate's own `manifests` step, scripts/gate.sh step_manifests, so a developer's
# shell and the gate see the identical result), and the T16.1.2 "aks has no dev-only flag"
# Done-when. The real in-cluster smoke (local overlay actually comes up) is
# scripts/k8s/smoke-16-1-cluster.sh, run separately against a live k3d cluster.
#
#   scripts/k8s/smoke-16-1.sh
#
# --load-restrictor LoadRestrictionsNone is required: deploy/k8s/base/jobs's roles-scripts
# configMapGenerator reads db/roles.sql + scripts/db/apply_roles.sh + scripts/db/scram_verifier.pl
# from outside deploy/k8s/** BY DESIGN (DEC-051 -- the roles Job must package the repo's one real
# roles.sql, never a duplicate).
#
# kube-linter runs with deploy/k8s/.kube-linter.yaml (T16.1.7): a project-wide, dated exception
# for the two checks (run-as-non-root, no-read-only-root-fs) that T16.4.2 (block 16-E, Pod
# Security `restricted`) fixes together -- see that file's own header for the full rationale.
# Every other finding is a real failure, including on a check this file does not yet know
# about (a future kube-linter default-check addition shows up here, not silently).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OVERLAYS_DIR="$REPO_ROOT/deploy/k8s/overlays"
pass=0
fail=0

ok()  { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1: $2"; fail=$((fail + 1)); }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

build() {
  # build <overlay> <out-file>
  if kustomize build --load-restrictor LoadRestrictionsNone "$OVERLAYS_DIR/$1" > "$2" 2>"$tmp/$1.err"; then
    return 0
  fi
  return 1
}

for overlay in local aks; do
  out="$tmp/$overlay.yaml"
  if build "$overlay" "$out"; then
    ok "kustomize-build-$overlay"
  else
    bad "kustomize-build-$overlay" "$(cat "$tmp/$overlay.err")"
    continue
  fi

  if kubeconform -strict -summary -ignore-missing-schemas "$out" >"$tmp/$overlay.kubeconform" 2>&1; then
    ok "kubeconform-$overlay ($(tail -1 "$tmp/$overlay.kubeconform"))"
  else
    bad "kubeconform-$overlay" "$(cat "$tmp/$overlay.kubeconform")"
  fi

  # kube-linter: ENFORCED (T16.1.7) with the project's own config (exceptions documented in
  # deploy/k8s/.kube-linter.yaml and, per-object, deploy/k8s/base/ollama/service.yaml).
  if kube-linter lint --config "$REPO_ROOT/deploy/k8s/.kube-linter.yaml" "$out" >"$tmp/$overlay.kubelinter" 2>&1; then
    ok "kube-linter-$overlay (0 findings)"
  else
    bad "kube-linter-$overlay" "$(cat "$tmp/$overlay.kubelinter")"
  fi
done

# T16.1.2 Done-when: the aks overlay carries no dev-only flag.
if [ -s "$tmp/aks.yaml" ]; then
  if grep -qE '^\s*ENV:\s*dev\s*$|LLM_PROVIDER:\s*fake' "$tmp/aks.yaml"; then
    bad "aks-no-dev-flags" "found a dev-only flag in the rendered aks overlay"
  else
    ok "aks-no-dev-flags"
  fi
fi

# Sanity: the local overlay DOES opt into ENV=dev (the opposite assertion -- catches a future
# edit accidentally deleting the local patch).
if [ -s "$tmp/local.yaml" ]; then
  if grep -qE '^\s*ENV:\s*dev\s*$' "$tmp/local.yaml"; then
    ok "local-has-dev-env"
  else
    bad "local-has-dev-env" "expected ENV: dev somewhere in the rendered local overlay"
  fi
fi

echo "---"
echo "smoke-16-1: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
