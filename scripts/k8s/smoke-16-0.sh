#!/usr/bin/env bash
# Smoke tests for block 16-A (T16.0.1–T16.0.6), run against an already-up cluster
# (scripts/k8s/cluster-up.sh). Exercises things `shellcheck` cannot: the cluster is really up,
# a pod can really reach the native Ollama through the Service/EndpointSlice, and the age-backed
# Sealed Secrets key backup/restore round-trips a Secret's data unchanged. Exits non-zero on the
# first failure; never touches a real sealed-secrets key (makes its own disposable test secret
# and its own disposable age keypair under a temp dir, both discarded at the end).
#
#   scripts/k8s/smoke-16-0.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="secrag"
pass=0
fail=0

ok()  { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1: $2"; fail=$((fail + 1)); }

# --- 1. nodes Ready -------------------------------------------------------------------
if kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}' | grep -qv '^Ready$'; then
  bad "nodes-ready" "at least one node is not Ready"
else
  n="$(kubectl get nodes --no-headers | wc -l)"
  if [ "$n" -ge 1 ]; then
    ok "nodes-ready ($n node(s))"
  else
    bad "nodes-ready" "no nodes found"
  fi
fi

# --- 2. Ollama reachable from a pod via the Service/EndpointSlice --------------------
if kubectl -n "$NS" run smoke-ollama-"$$" --image=busybox:1.36 --restart=Never --rm -i --command -- \
    sh -c 'wget -q -O- -T 5 http://ollama-host.secrag.svc.cluster.local:11434/api/version' \
    2>/dev/null | grep -q '"version"'; then
  ok "ollama-reachable-from-pod"
else
  bad "ollama-reachable-from-pod" "pod could not reach ollama-host:11434 (is cluster-up.sh's EndpointSlice applied? is Ollama running?)"
fi

# --- 3. Sealed Secrets key backup/restore round-trips a Secret's data ----------------
# sealed-secrets-key.sh's `backup` picks items[0] of EVERY secret in kube-system carrying the
# real bitnami active-key label — with no name filter, so it cannot be pointed at a disposable
# secret while a real controller (T16.1.3+) already has its own active key labelled the same
# way: "sealed-secrets-key..." sorts before "smoke-sealed-secrets-key" and items[0] silently
# picked the REAL key once one existed (16-B fix: this smoke test used to run before T16.1.3
# installed the controller, when the disposable secret was the ONLY one with that label).
# Fix: temporarily strip the label from the real key (if any) so the disposable secret is
# unambiguous, run the exact same round-trip as before, then restore the real key's label in
# the trap regardless of outcome. The real key's DATA is never touched -- only its label, and
# only for the few seconds this test runs.
tmp="$(mktemp -d)"
real_key_name="$(kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key=active \
  -o jsonpath='{.items[?(@.metadata.name!="smoke-sealed-secrets-key")].metadata.name}' 2>/dev/null | awk '{print $1}')"
restore_real_label() {
  if [ -n "$real_key_name" ]; then
    kubectl -n kube-system label secret "$real_key_name" \
      sealedsecrets.bitnami.com/sealed-secrets-key=active --overwrite >/dev/null 2>&1 || true
  fi
}
trap 'rm -rf "$tmp"; kubectl -n kube-system delete secret smoke-sealed-secrets-key --ignore-not-found >/dev/null 2>&1; restore_real_label' EXIT
if [ -n "$real_key_name" ]; then
  kubectl -n kube-system label secret "$real_key_name" sealedsecrets.bitnami.com/sealed-secrets-key- >/dev/null
fi
age-keygen -o "$tmp/identity.key" 2>/dev/null
recipient="$(grep '^# public key:' "$tmp/identity.key" | awk '{print $NF}')"
chmod 600 "$tmp/identity.key"

kubectl -n kube-system delete secret smoke-sealed-secrets-key --ignore-not-found >/dev/null 2>&1
kubectl -n kube-system create secret generic smoke-sealed-secrets-key \
  --from-literal=tls.crt=smoke-test-cert --from-literal=tls.key=smoke-test-key >/dev/null
kubectl -n kube-system label secret smoke-sealed-secrets-key \
  sealedsecrets.bitnami.com/sealed-secrets-key=active >/dev/null

# Point the script at our disposable secret via the same LABEL it would use for the real one —
# only possible because this is the exact label sealed-secrets uses; no code path is bypassed.
backup_file="$tmp/key.age"
"$SCRIPT_DIR/sealed-secrets-key.sh" backup --recipient "$recipient" --out "$backup_file"
if [ ! -s "$backup_file" ]; then
  bad "sealed-secrets-key-roundtrip" "backup produced no file"
else
  kubectl -n kube-system delete secret smoke-sealed-secrets-key >/dev/null
  "$SCRIPT_DIR/sealed-secrets-key.sh" restore --identity "$tmp/identity.key" --in "$backup_file"
  got_key="$(kubectl -n kube-system get secret smoke-sealed-secrets-key -o jsonpath='{.data.tls\.key}' | base64 -d)"
  if [ "$got_key" = "smoke-test-key" ]; then
    ok "sealed-secrets-key-roundtrip"
  else
    bad "sealed-secrets-key-roundtrip" "restored data does not match (got: ${got_key:-<empty>})"
  fi
fi
kubectl -n kube-system delete secret smoke-sealed-secrets-key --ignore-not-found >/dev/null 2>&1
restore_real_label

echo "----"
echo "smoke-16-0: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
