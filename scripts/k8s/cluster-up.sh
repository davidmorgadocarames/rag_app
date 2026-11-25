#!/usr/bin/env bash
# Bring up the local k3d cluster for Phase 16 (T16.0.3): 1 server + 2 agents, Traefik disabled
# (Envoy Gateway takes over in T16.1.1), a local image registry, Docker memory limits per node
# (T16.2.2, 2026-11-25, self-reported fix: the original 2g server / 1g-per-agent split (16-A)
# left only 1Gi allocatable per agent node -- too small to schedule even ONE backend pod at this
# block's own MEASURED `requests.memory: 2Gi`, confirmed live: `0/3 nodes are available: 3
# Insufficient memory` with the old caps. Raised to 2g/3g/3g = 8GB total, matching the
# internal planning docs' own already-stated "Resource budget (local)" figure for Phase 16 --
# that figure was never actually realised by 16-A's k3d flags until now),
# the `secrag` namespace, the hand-managed Ollama Service/EndpointSlice/NetworkPolicy
# (T16.0.4, reusing the T11.0.12 compose pattern for reaching the host's native Ollama), and
# (T16.1.3/T16.1.4, 16-B living-graph deviation -- see status/blocks/16-B.md) three pinned Helm
# controller installs: Envoy Gateway, Sealed Secrets, CloudNativePG. These are cluster-level
# infrastructure (same category as the registry/Traefik-disable above), not application
# manifests -- kept OUT of deploy/k8s/base so `kustomize build` stays deterministic and
# network-free (no Helm chart fetch at build time). deploy/k8s/base's own Gateway/SealedSecret/
# Cluster CRs are applied separately by scripts/k8s/deploy-local.sh (T16.1.4+), after these
# controllers + their CRDs exist.
# Idempotent: re-running against an already-up cluster only re-applies the manifests/charts +
# restores the Sealed Secrets key, it does not recreate the cluster.
#
#   scripts/k8s/cluster-up.sh
#
# Env (both optional):
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
    --agents-memory 3g \
    --wait --timeout 180s
fi

log "waiting for all nodes to be Ready"
kubectl wait --for=condition=Ready nodes --all --timeout=180s

command -v helm >/dev/null || die "helm not found — run scripts/k8s/install-tools.sh first"

# Envoy Gateway (P16-D2). OCI chart, pinned by version (the chart artifact's own digest at pin
# time, recorded for auditability: sha256:62fe8cca72a8770bb33900dd5c0990011ab98f2d6f4420c449ce44b1ca4eebe6).
log "installing/upgrading the Envoy Gateway controller (chart 1.10.0)"
helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm --version 1.10.0 \
  -n envoy-gateway-system --create-namespace --wait --timeout 180s >/dev/null
kubectl wait --for=condition=Available deployment/envoy-gateway -n envoy-gateway-system --timeout=180s

# Sealed Secrets (T16.1.3). Chart 2.22.0 / controller app version 0.41.2 (image digest at pin
# time: sha256:e63ec6ec2b12a471bf781081bd370a93ca7b7b3999e5b9243341379aeb801d0b).
log "installing/upgrading the Sealed Secrets controller (chart 2.22.0)"
helm repo add sealed-secrets https://bitnami.github.io/sealed-secrets --force-update >/dev/null
helm repo update sealed-secrets >/dev/null
helm upgrade --install sealed-secrets sealed-secrets/sealed-secrets --version 2.22.0 \
  -n kube-system --wait --timeout 120s >/dev/null

# CloudNativePG operator (T16.1.4). Chart 0.29.2 / operator app version 1.30.1 (image digest at
# pin time: sha256:923c267ec29636db3bee20f993d0ec4973fa22998e1adad37da79e4d32b5bc07).
log "installing/upgrading the CloudNativePG operator (chart 0.29.2)"
helm repo add cnpg https://cloudnative-pg.github.io/charts --force-update >/dev/null
helm repo update cnpg >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg --version 0.29.2 \
  -n cnpg-system --create-namespace --wait --timeout 180s >/dev/null

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
