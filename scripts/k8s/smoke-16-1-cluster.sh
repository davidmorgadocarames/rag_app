#!/usr/bin/env bash
# In-cluster smoke for block 16-B/16-B-fix (T16.1.1-T16.1.6): "the local overlay comes up"
# against an already-up cluster (scripts/k8s/cluster-up.sh, which installs the Envoy Gateway/
# Sealed Secrets/CloudNativePG controllers). Applies the local overlay for real -- via
# scripts/k8s/deploy-local.sh (DA-16-B-1 fix: the apply-ordering script, not a flat
# `kustomize build | kubectl apply` -- the ordering itself is this script's own regression test,
# see backend/tests/test_k8s_deploy_order.py for the offline proof) -- and checks every piece
# that `kustomize build`/kubeconform/kube-linter (scripts/k8s/smoke-16-1.sh) cannot: SealedSecrets
# really decrypt, CNPG really reaches "Cluster in healthy state" with pgvector installed, the
# roles Job really runs db/roles.sql against it, and the in-cluster services (Valkey, SeaweedFS,
# kube-state-metrics, Prometheus, Mailpit) really start and (for Prometheus) really scrape
# kube-state-metrics successfully.
#
# X1 placeholder-image note: the backend/frontend/jobs images are all still `:placeholder`, so
# the migrate Job can never pull and deploy-local.sh correctly fail-closes (exit code 6,
# EXIT_MIGRATE_JOB_FAILED) BEFORE ever applying the backend/frontend Deployments -- that specific
# exit code is treated as an expected, documented X1 outcome below, not a smoke failure; any
# OTHER exit code from deploy-local.sh is. Building real app images is a separate CI/CD concern
# outside this block.
#
#   scripts/k8s/smoke-16-1-cluster.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="secrag"
EXIT_MIGRATE_JOB_FAILED=6   # must stay in sync with deploy-local.sh's own exit code
pass=0
fail=0

ok()  { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1: $2"; fail=$((fail + 1)); }

kubectl get namespace >/dev/null 2>&1 || { echo "no cluster reachable -- run cluster-up.sh first" >&2; exit 2; }

echo "[smoke-16-1-cluster] applying the local overlay via scripts/k8s/deploy-local.sh (ordered)"
deploy_rc=0
MIGRATE_TIMEOUT="${MIGRATE_TIMEOUT:-20s}" "$SCRIPT_DIR/deploy-local.sh" || deploy_rc=$?
case "$deploy_rc" in
  0)
    ok "deploy-local-apply-order (full success, including backend/frontend rollout)"
    ;;
  "$EXIT_MIGRATE_JOB_FAILED")
    ok "deploy-local-apply-order (migrate Job did not complete -- expected with the X1 placeholder jobs image; backend/frontend correctly NOT applied)"
    ;;
  *)
    bad "deploy-local-apply-order" "deploy-local.sh exited $deploy_rc (expected 0 or $EXIT_MIGRATE_JOB_FAILED)"
    ;;
esac

echo "[smoke-16-1-cluster] roles Job (already applied+waited by deploy-local.sh -- re-checking, not re-applying)"
if [ "$(kubectl -n "$NS" get job roles -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null)" = "True" ]; then
  ok "roles-job-completes"
else
  bad "roles-job-completes" "$(kubectl -n "$NS" logs job/roles 2>&1 | tail -5)"
fi

echo "[smoke-16-1-cluster] CNPG cluster (already applied+waited by deploy-local.sh -- re-checking, not re-applying)"
if kubectl -n "$NS" get cluster secrag-pg -o jsonpath='{.status.phase}' 2>/dev/null | grep -q "Cluster in healthy state"; then
  ok "cnpg-cluster-ready"
else
  bad "cnpg-cluster-ready" "$(kubectl -n "$NS" get cluster secrag-pg -o jsonpath='{.status.phase}')"
fi

ext="$(kubectl -n "$NS" exec secrag-pg-1 -c postgres -- psql -U postgres -d rag -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='vector'" 2>/dev/null || true)"
if [ -n "$ext" ]; then
  ok "pgvector-installed ($ext)"
else
  bad "pgvector-installed" "pg_extension has no row for 'vector'"
fi

roles_present="$(kubectl -n "$NS" exec secrag-pg-1 -c postgres -- psql -U postgres -d rag -tAc \
  "SELECT count(*) FROM pg_roles WHERE rolname IN ('secrag_purger','secrag_backup')" 2>/dev/null || echo 0)"
if [ "$roles_present" = "2" ]; then
  ok "db-roles-applied"
else
  bad "db-roles-applied" "expected 2 roles (secrag_purger, secrag_backup), found $roles_present"
fi

echo "[smoke-16-1-cluster] T16.1.6 CronJobs exist with the right schedule/suspend state"
purge_schedule="$(kubectl -n "$NS" get cronjob purge -o jsonpath='{.spec.schedule}' 2>/dev/null || true)"
if [ "$purge_schedule" = "0 * * * *" ]; then
  ok "purge-cronjob-hourly"
else
  bad "purge-cronjob-hourly" "schedule is '$purge_schedule', expected '0 * * * *'"
fi
audit_suspended="$(kubectl -n "$NS" get cronjob audit-retention -o jsonpath='{.spec.suspend}' 2>/dev/null || true)"
if [ "$audit_suspended" = "true" ]; then
  ok "audit-retention-cronjob-suspended-as-documented"
else
  bad "audit-retention-cronjob-suspended-as-documented" "suspend='$audit_suspended', expected 'true' (no audit_log table until Phase 15)"
fi
cnpg_sched_backup="$(kubectl -n "$NS" get scheduledbackup secrag-pg-backup -o jsonpath='{.spec.cluster.name}' 2>/dev/null || true)"
if [ "$cnpg_sched_backup" = "secrag-pg" ]; then
  ok "cnpg-scheduledbackup-targets-secrag-pg"
else
  bad "cnpg-scheduledbackup-targets-secrag-pg" "got '$cnpg_sched_backup'"
fi

echo "[smoke-16-1-cluster] waiting for in-cluster services"
for svc in mailpit valkey seaweedfs kube-state-metrics prometheus; do
  if kubectl -n "$NS" wait --for=condition=Available "deployment/$svc" --timeout=120s >/dev/null 2>&1; then
    ok "service-available-$svc"
  else
    bad "service-available-$svc" "$(kubectl -n "$NS" get pods -l app.kubernetes.io/name="$svc" --no-headers)"
  fi
done

echo "[smoke-16-1-cluster] SealedSecrets really decrypted"
for name in app-secrets secrag-db-credentials seaweedfs-credentials seaweedfs-s3-config valkey-credentials; do
  n="$(kubectl -n "$NS" get secret "$name" -o jsonpath='{.data}' 2>/dev/null | grep -c ':' || true)"
  if [ "$n" -ge 1 ]; then
    ok "sealed-secret-decrypted-$name"
  else
    bad "sealed-secret-decrypted-$name" "Secret missing or empty"
  fi
done

echo "[smoke-16-1-cluster] Prometheus scrapes kube-state-metrics"
targets_json="$(kubectl -n "$NS" exec deploy/prometheus -- wget -qO- http://localhost:9090/api/v1/targets 2>/dev/null || true)"
if echo "$targets_json" | grep -q '"job":"kube-state-metrics".*"health":"up"'; then
  ok "prometheus-scrapes-kube-state-metrics"
else
  bad "prometheus-scrapes-kube-state-metrics" "kube-state-metrics target not healthy"
fi

echo "[smoke-16-1-cluster] SeaweedFS S3 gateway answers (auth-enforced, not plumbing-broken)"
s3_code="$(kubectl -n "$NS" run smoke-s3-"$$" --image=curlimages/curl:8.11.1 --restart=Never --rm -i --command -- \
  curl -s -o /dev/null -w '%{http_code}\n' http://seaweedfs.secrag.svc.cluster.local:8333/ 2>/dev/null \
  | grep -oE '^[0-9]{3}$' | head -1)"
if [ "$s3_code" = "403" ] || [ "$s3_code" = "200" ]; then
  ok "seaweedfs-s3-reachable (HTTP $s3_code)"
else
  bad "seaweedfs-s3-reachable" "unexpected HTTP $s3_code"
fi

echo "----"
echo "smoke-16-1-cluster: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
