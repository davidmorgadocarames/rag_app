#!/usr/bin/env bash
# Tear down the local k3d cluster for Phase 16 (T16.0.3). Backs up the Sealed Secrets key first
# (no-op before T16.1.3, when the controller does not exist yet) so a later cluster-up.sh can
# restore it. Leaves nothing behind: the cluster, its nodes and its k3d-managed registry are all
# deleted (k3d ties the registry's lifecycle to the cluster that created it with
# --registry-create; `k3d registry list` after this should be empty).
#
#   scripts/k8s/cluster-down.sh
#
# Env (optional, see cluster-up.sh — only meaningful once T16.1.3 installs the controller):
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

log "teardown complete"
k3d cluster list
k3d registry list
