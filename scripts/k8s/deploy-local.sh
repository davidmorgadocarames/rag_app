#!/usr/bin/env bash
# The apply-ordering script the manifests' own comments already promised (T16.1.1/T16.1.6,
# DA-16-B-1 fix): deploy/k8s/base/jobs/migrate-job.yaml, roles-job.yaml,
# deploy/k8s/base/backend/deployment.yaml and scripts/k8s/cluster-up.sh all say this script
# enforces "CNPG healthy -> roles Job -> migrate Job -> apps" -- before this file existed, that
# was a false claim (DA-16-B.md's blocker). This script IS that enforcement, nothing else: it
# does NOT build or push any image (the backend/frontend/jobs image tags are still the X1
# PLACEHOLDER convention until a later block/CI writes the real build+push step) and it does NOT
# install the Envoy Gateway/Sealed Secrets/CloudNativePG controllers (cluster-up.sh's job, via
# pinned Helm charts, before this script ever runs).
#
# Order enforced (real mode, cluster required):
#   1. apply the `secrag` Namespace
#   2. wait for the 3 controllers' CRDs to be Established + the controllers' Deployments to be
#      Available (pre-installed by cluster-up.sh -- this is a readiness check, not an install)
#   3. apply every other non-order-critical resource (SealedSecrets, ConfigMaps, RBAC, Services,
#      CronJobs, the Gateway/HTTPRoute stack) -- none of these has a correctness dependency on
#      the stages below, so they are applied together right after the controllers are confirmed
#      ready
#   4. apply the CNPG `Cluster` + `ScheduledBackup`; wait for the Cluster to be Ready
#   5. apply the `roles` Job; wait for it to Complete (fail-closed: exits non-zero, never
#      reaches the stages below, if it does not complete in time)
#   6. apply the `migrate` Job; wait for it to Complete (fail-closed, same as above -- this is
#      the exact gap DA-16-B-1 found: nothing used to stop the backend from rolling out before
#      this finished)
#   7. apply the backend/frontend Deployments; `kubectl rollout status` both
#
# X1 placeholder-image note: today the backend/frontend/jobs images are all still
# `:placeholder` (ErrImagePull/ImagePullBackOff), so step 6 above will legitimately time out and
# fail-closed on a real cluster run until a later block builds+pushes real images -- that is the
# CORRECT behaviour this script exists to provide (it must refuse to roll out the backend before
# migrate completes, not "usually" refuse). scripts/k8s/smoke-16-1-cluster.sh calls this script
# with a short MIGRATE_TIMEOUT and treats exit code 6 (EXIT_MIGRATE_JOB_FAILED) as the expected,
# documented X1 outcome today, not a smoke failure.
#
# Idempotent: safe to re-run against an already-deployed cluster (every `kubectl apply` and
# `kubectl wait`/`kubectl rollout status` call is itself idempotent).
#
# Usage:
#   scripts/k8s/deploy-local.sh                 # real run against the current kube context
#   scripts/k8s/deploy-local.sh --dry-run [--trace FILE]
#       Proves the apply/wait ORDER without touching a cluster or running kustomize at all (no
#       cluster, no kustomize binary needed) -- every stage function below still runs, in the
#       same order a real run would use, it just skips the actual kubectl call. --trace FILE
#       appends one keyword per stage to FILE, in order, for a test to assert on
#       (backend/tests/test_k8s_deploy_order.py). Always exits 0.
#
# Env (all optional, defaults shown):
#   CONTROLLER_TIMEOUT=60s   CNPG_TIMEOUT=180s   ROLES_TIMEOUT=120s
#   MIGRATE_TIMEOUT=120s     ROLLOUT_TIMEOUT=180s
set -euo pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OVERLAY_DIR="$REPO_ROOT/deploy/k8s/overlays/local"
NS="secrag"

CONTROLLER_TIMEOUT="${CONTROLLER_TIMEOUT:-60s}"
CNPG_TIMEOUT="${CNPG_TIMEOUT:-180s}"
ROLES_TIMEOUT="${ROLES_TIMEOUT:-120s}"
MIGRATE_TIMEOUT="${MIGRATE_TIMEOUT:-120s}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"

EXIT_USAGE=1
EXIT_NO_CLUSTER=2
EXIT_CONTROLLERS_NOT_READY=3
EXIT_CNPG_NOT_HEALTHY=4
EXIT_ROLES_JOB_FAILED=5
EXIT_MIGRATE_JOB_FAILED=6
EXIT_ROLLOUT_FAILED=7

DRY_RUN=0
TRACE_FILE=""
render_dir=""

log() { echo "[deploy-local] $*"; }
die() { echo "[deploy-local] ERROR: $*" >&2; exit "$EXIT_USAGE"; }

usage() {
  cat <<'EOF' >&2
usage: scripts/k8s/deploy-local.sh [--dry-run] [--trace FILE]
EOF
}

trace() {  # $1 = keyword, appended in order if --trace FILE was given
  # NOTE: must always return 0 -- under `set -e`, a bare `[ -n "$TRACE_FILE" ] && …` as the last
  # statement returns the test's own (non-zero) status when TRACE_FILE is unset, which aborted
  # the whole script the instant any stage called trace() without --trace (caught by the real
  # cluster run below, not by the --dry-run --trace test, which always set TRACE_FILE).
  if [ -n "$TRACE_FILE" ]; then
    printf '%s\n' "$1" >>"$TRACE_FILE"
  fi
  return 0
}

fail_closed() {  # $1 = exit code, $2.. = message
  local code="$1"
  shift
  log "FAIL-CLOSED: $*"
  exit "$code"
}

# do_apply KEYWORD [FILE...] -- applies FILEs (if any) unless --dry-run.
do_apply() {
  local kw="$1"
  shift
  log "apply: $kw"
  trace "APPLY_$kw"
  [ "$DRY_RUN" -eq 1 ] && return 0
  if [ "$#" -eq 0 ]; then
    log "nothing to apply for $kw (0 matching documents in this render)"
    return 0
  fi
  local args=()
  local f
  for f in "$@"; do
    args+=(-f "$f")
  done
  kubectl apply "${args[@]}" >/dev/null
}

# do_check KEYWORD EXIT_CODE KUBECTL_SUBCOMMAND... -- runs `kubectl $@`; fail-closed on error.
do_check() {
  local kw="$1" code="$2"
  shift 2
  log "check: $kw"
  trace "CHECK_$kw"
  [ "$DRY_RUN" -eq 1 ] && return 0
  kubectl "$@" >/dev/null || fail_closed "$code" "$kw did not reach the expected state in time"
}

require_cluster() {
  kubectl get namespace >/dev/null 2>&1 \
    || { echo "[deploy-local] no cluster reachable -- run cluster-up.sh first" >&2; exit "$EXIT_NO_CLUSTER"; }
}

# build_index SRC_FILE DEST_DIR -- splits a multi-document kustomize-build render (SRC_FILE)
# into one file per document under DEST_DIR, and writes DEST_DIR/index.txt with one
# "path:kind:name" line per document (no yq in this environment -- awk/grep only, same as the
# rest of this repo's k8s scripts).
build_index() {
  local src="$1" dir="$2"
  awk -v dir="$dir" '
    BEGIN { n = 0; out = sprintf("%s/doc-%04d.yaml", dir, n) }
    /^---[[:space:]]*$/ { n++; out = sprintf("%s/doc-%04d.yaml", dir, n); next }
    { print > out }
  ' "$src"
  local f kind name
  {
    for f in "$dir"/doc-*.yaml; do
      [ -s "$f" ] || continue
      kind=$(awk -F': *' '/^kind:/ {print $2; exit}' "$f")
      name=$(awk -F': *' '/^  name:/ {print $2; exit}' "$f")
      printf '%s:%s:%s\n' "$f" "$kind" "$name"
    done
  } >"$dir/index.txt"
}

stage_namespace() {
  local files=()
  if [ "$DRY_RUN" -eq 0 ]; then
    mapfile -t files < <(awk -F: '$2 == "Namespace" {print $1}' "$render_dir/index.txt")
  fi
  do_apply NAMESPACE "${files[@]}"
}

stage_controllers_ready() {
  log "check: CONTROLLERS_READY"
  trace "CHECK_CONTROLLERS_READY"
  [ "$DRY_RUN" -eq 1 ] && return 0
  local crd
  for crd in sealedsecrets.bitnami.com clusters.postgresql.cnpg.io scheduledbackups.postgresql.cnpg.io \
    gatewayclasses.gateway.networking.k8s.io gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io; do
    kubectl wait --for=condition=Established "crd/$crd" --timeout="$CONTROLLER_TIMEOUT" >/dev/null \
      || fail_closed "$EXIT_CONTROLLERS_NOT_READY" "CRD $crd is not Established -- run cluster-up.sh first"
  done
  kubectl wait --for=condition=Available deployment/envoy-gateway -n envoy-gateway-system --timeout="$CONTROLLER_TIMEOUT" >/dev/null \
    || fail_closed "$EXIT_CONTROLLERS_NOT_READY" "envoy-gateway controller is not Available"
  kubectl wait --for=condition=Available deployment/sealed-secrets -n kube-system --timeout="$CONTROLLER_TIMEOUT" >/dev/null \
    || fail_closed "$EXIT_CONTROLLERS_NOT_READY" "sealed-secrets controller is not Available"
  kubectl wait --for=condition=Available deployment/cnpg-cloudnative-pg -n cnpg-system --timeout="$CONTROLLER_TIMEOUT" >/dev/null \
    || fail_closed "$EXIT_CONTROLLERS_NOT_READY" "cnpg-cloudnative-pg controller is not Available"
}

# Every resource below has no correctness dependency on the CNPG/roles/migrate/apps stages --
# applied together right after the controllers are confirmed ready.
# PodDisruptionBudget added here T16.2.3 (self-reported gap found by this block's own node-drain
# chaos drill: `deploy-local.sh` never applied the backend/frontend PDBs at all because this
# regex predates them, which let a real `kubectl drain` evict the backend pod despite the
# committed manifest declaring a PDB that should have blocked it -- the PDB existed in git but
# was never actually live on a cluster deployed via this script).
stage_foundation() {
  local files=()
  if [ "$DRY_RUN" -eq 0 ]; then
    mapfile -t files < <(awk -F: '
      $2 ~ /^(ServiceAccount|ClusterRole|ClusterRoleBinding|ConfigMap|Service|SealedSecret|CronJob|Gateway|GatewayClass|HTTPRoute|PodDisruptionBudget)$/ { print $1; next }
      $2 == "Deployment" && $3 != "backend" && $3 != "frontend" { print $1 }
    ' "$render_dir/index.txt")
  fi
  do_apply FOUNDATION "${files[@]}"
}

stage_cnpg() {
  local files=()
  if [ "$DRY_RUN" -eq 0 ]; then
    mapfile -t files < <(awk -F: '$2 == "Cluster" || $2 == "ScheduledBackup" {print $1}' "$render_dir/index.txt")
  fi
  do_apply CNPG "${files[@]}"
  do_check CNPG_HEALTHY "$EXIT_CNPG_NOT_HEALTHY" wait -n "$NS" --for=condition=Ready cluster/secrag-pg --timeout="$CNPG_TIMEOUT"
}

stage_roles_job() {
  local files=()
  if [ "$DRY_RUN" -eq 0 ]; then
    mapfile -t files < <(awk -F: '$2 == "Job" && $3 == "roles" {print $1}' "$render_dir/index.txt")
  fi
  do_apply ROLES_JOB "${files[@]}"
  do_check ROLES_JOB_COMPLETE "$EXIT_ROLES_JOB_FAILED" wait -n "$NS" --for=condition=Complete job/roles --timeout="$ROLES_TIMEOUT"
}

stage_migrate_job() {
  local files=()
  if [ "$DRY_RUN" -eq 0 ]; then
    mapfile -t files < <(awk -F: '$2 == "Job" && $3 == "migrate" {print $1}' "$render_dir/index.txt")
  fi
  do_apply MIGRATE_JOB "${files[@]}"
  do_check MIGRATE_JOB_COMPLETE "$EXIT_MIGRATE_JOB_FAILED" wait -n "$NS" --for=condition=Complete job/migrate --timeout="$MIGRATE_TIMEOUT"
}

stage_apps() {
  local files=()
  if [ "$DRY_RUN" -eq 0 ]; then
    mapfile -t files < <(awk -F: '$2 == "Deployment" && ($3 == "backend" || $3 == "frontend") {print $1}' "$render_dir/index.txt")
  fi
  do_apply APPS "${files[@]}"
  do_check ROLLOUT_BACKEND "$EXIT_ROLLOUT_FAILED" rollout status -n "$NS" deployment/backend --timeout="$ROLLOUT_TIMEOUT"
  do_check ROLLOUT_FRONTEND "$EXIT_ROLLOUT_FAILED" rollout status -n "$NS" deployment/frontend --timeout="$ROLLOUT_TIMEOUT"
}

main() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --trace)
        [ -n "${2:-}" ] || die "--trace needs a FILE argument"
        TRACE_FILE="$2"
        : >"$TRACE_FILE"
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage
        die "unknown argument: $1"
        ;;
    esac
  done

  if [ "$DRY_RUN" -eq 0 ]; then
    require_cluster
    render_dir="$(mktemp -d)"
    trap 'rm -rf "$render_dir"' EXIT
    kustomize build --load-restrictor LoadRestrictionsNone "$OVERLAY_DIR" >"$render_dir/all.yaml"
    build_index "$render_dir/all.yaml" "$render_dir"
  fi

  stage_namespace
  stage_controllers_ready
  stage_foundation
  stage_cnpg
  stage_roles_job
  stage_migrate_job
  stage_apps
  log "done"
}

main "$@"
