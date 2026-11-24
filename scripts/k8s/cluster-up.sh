#!/usr/bin/env bash
# Bring up the local k3d cluster for Phase 16 (T16.0.3): 1 server + 2 agents, Traefik disabled
# (Envoy Gateway takes over in T16.1.1), a local image registry, Docker memory limits per node,
# the `secrag` namespace, and the hand-managed Ollama Service/EndpointSlice/NetworkPolicy
# (T16.0.4, reusing the T11.0.12 compose pattern for reaching the host's native Ollama).
# Idempotent: re-running against an already-up cluster only re-applies the manifests + restores
# the Sealed Secrets key, it does not recreate the cluster.
#
#   scripts/k8s/cluster-up.sh
#
# Env (both optional — the Sealed Secrets controller does not exist until T16.1.3, so neither
# is needed yet; set them once that lands if you want the key preserved across teardowns):
#   SEALED_SECRETS_AGE_IDENTITY   private key file used to decrypt the backed-up key (restore)
#   SEALED_SECRETS_AGE_BACKUP     backup file path (default: ~/.secrag-k8s/sealed-secrets-key.age)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CLUSTER="secrag"
REGISTRY="secrag-registry:0.0.0.0:5050"
NAMESPACE="secrag"

log() { echo "[cluster-up] $*"; }
die() { echo "[cluster-up] ERROR: $*" >&2; exit 1; }

command -v k3d >/dev/null || die "k3d not found — run scripts/k8s/install-tools.sh first"
command -v kubectl >/dev/null || die "kubectl not found — run scripts/k8s/install-tools.sh first"
docker info >/dev/null 2>&1 || die "Docker is not reachable — start Docker Desktop first"

if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  log "cluster '$CLUSTER' already exists — starting it (no recreate)"
  k3d cluster start "$CLUSTER"
else
  log "creating cluster '$CLUSTER' (1 server + 2 agents, Traefik disabled, registry, memory limits)"
  k3d cluster create "$CLUSTER" \
    --servers 1 --agents 2 \
    --k3s-arg '--disable=traefik@server:0' \
    --registry-create "$REGISTRY" \
    --servers-memory 2g \
    --agents-memory 1g \
    --wait --timeout 180s
fi

log "waiting for all nodes to be Ready"
kubectl wait --for=condition=Ready nodes --all --timeout=180s

log "applying the $NAMESPACE namespace"
kubectl apply -f "$REPO_ROOT/deploy/k8s/base/namespace.yaml"

log "applying the ollama-host Service (static part)"
kubectl apply -k "$REPO_ROOT/deploy/k8s/base/ollama"

# Resolve the host's Ollama IP exactly as the k3d server node container itself resolves it —
# that container's /etc/resolv.conf is the upstream CoreDNS forwards to, so this is the same
# answer a pod gets when it looks up `host.docker.internal` from inside the cluster. The k3s
# node image has no `getent`, only busybox's `nslookup`; take the first plain IPv4
# "Address: a.b.c.d" line under the answer section (skips the "Server:"/":53" line and any
# IPv6 answer).
server_container="k3d-$CLUSTER-server-0"
host_ip="$(docker exec "$server_container" nslookup host.docker.internal 2>/dev/null \
  | awk '/^Address: [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $2; exit}')"
[ -n "$host_ip" ] || die "could not resolve host.docker.internal from inside $server_container"
log "resolved host.docker.internal -> $host_ip (from inside $server_container)"

log "rendering + applying the ollama-host EndpointSlice and the backend-egress NetworkPolicy"
# shellcheck disable=SC2016  # envsubst's own var-list argument, not shell expansion
OLLAMA_HOST_IP="$host_ip" envsubst '${OLLAMA_HOST_IP}' \
  < "$REPO_ROOT/deploy/k8s/base/ollama/endpointslice.yaml.tmpl" | kubectl apply -f -
# shellcheck disable=SC2016  # envsubst's own var-list argument, not shell expansion
OLLAMA_HOST_IP="$host_ip" envsubst '${OLLAMA_HOST_IP}' \
  < "$REPO_ROOT/deploy/k8s/base/ollama/networkpolicy.yaml.tmpl" | kubectl apply -f -

if [ -n "${SEALED_SECRETS_AGE_IDENTITY:-}" ]; then
  log "restoring the Sealed Secrets key (if a backup exists)"
  args=(restore --identity "$SEALED_SECRETS_AGE_IDENTITY")
  [ -n "${SEALED_SECRETS_AGE_BACKUP:-}" ] && args+=(--in "$SEALED_SECRETS_AGE_BACKUP")
  "$SCRIPT_DIR/sealed-secrets-key.sh" "${args[@]}"
else
  log "SEALED_SECRETS_AGE_IDENTITY not set — skipping the Sealed Secrets key restore (fine before T16.1.3)"
fi

log "cluster ready:"
kubectl get nodes -o wide
