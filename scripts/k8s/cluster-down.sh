#!/usr/bin/env bash
# Tear down the local k3d cluster for Phase 16 (T16.0.3). Backs up the Sealed Secrets key first
# (a no-op if the controller is not installed, e.g. on a pre-T16.1.3 checkout) so a later
# cluster-up.sh can restore it. Leaves nothing behind: the cluster, its nodes, its k3d-managed
# registry and every Helm release installed by cluster-up.sh (Envoy Gateway, Sealed Secrets,
# CloudNativePG -- T16.1.3/T16.1.4) are all deleted with the cluster itself (k3d ties the
# registry's lifecycle to the cluster that created it with --registry-create; `k3d registry
# list` after this should be empty).
#
#   scripts/k8s/cluster-down.sh
#
# Env (optional, see cluster-up.sh — only meaningful once the Sealed Secrets controller is up):
#   SEALED_SECRETS_AGE_RECIPIENT   public key used to encrypt the backup
#   SEALED_SECRETS_AGE_BACKUP      backup file path (default: ~/.secrag-k8s/sealed-secrets-key.age)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="secrag"

log() { echo "[cluster-down] $*"; }

if ! k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  log "cluster '$CLUSTER' does not exist — nothing to do"
  exit 0
fi

if [ -n "${SEALED_SECRETS_AGE_RECIPIENT:-}" ]; then
  log "backing up the Sealed Secrets key (if it exists)"
  args=(backup --recipient "$SEALED_SECRETS_AGE_RECIPIENT")
  [ -n "${SEALED_SECRETS_AGE_BACKUP:-}" ] && args+=(--out "$SEALED_SECRETS_AGE_BACKUP")
  "$SCRIPT_DIR/sealed-secrets-key.sh" "${args[@]}"
else
  log "SEALED_SECRETS_AGE_RECIPIENT not set — skipping the Sealed Secrets key backup (fine before T16.1.3)"
fi

log "deleting cluster '$CLUSTER' (and its k3d-managed registry)"
k3d cluster delete "$CLUSTER"

# k3d cluster delete does not always remove the docker network it created (observed: a network
# left with no containers attached after a reused/recreated cluster) — remove it ourselves so
# "clean teardown" is actually true, but only when it is provably empty (never force-remove a
# network something else might still be using).
network="k3d-$CLUSTER"
if docker network inspect "$network" >/dev/null 2>&1; then
  containers="$(docker network inspect "$network" --format '{{len .Containers}}')"
  if [ "$containers" = "0" ]; then
    log "removing leftover empty docker network '$network'"
    docker network rm "$network" >/dev/null
  else
    log "docker network '$network' still has $containers container(s) attached — leaving it"
  fi
fi

log "teardown complete"
k3d cluster list
k3d registry list
