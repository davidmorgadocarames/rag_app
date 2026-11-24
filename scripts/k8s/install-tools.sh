#!/usr/bin/env bash
# Install the local Kubernetes toolchain for Phase 16 (T16.0.2) as user binaries (no sudo),
# under ~/.local/opt/<tool>-<version> with entry points in ~/.local/bin — same supply-chain
# model as scripts/prereqs/install.sh: every download is pinned to an exact version and
# verified with SHA-256 before it is unpacked; installs are atomic (staged, then renamed into
# place) so an interrupted run is never mistaken for a finished install.
#
# Tools: k3d, kubectl, helm, kustomize, k6, kubeconform, kube-linter, cosign, kubeseal (T16.1.3,
# 16-B living-graph deviation -- see status/blocks/16-B.md: needed to seal the local overlay's
# app secrets against the live Sealed Secrets controller; pinned to the SAME release as the
# controller chart's app version, 0.41.2).
#
#   scripts/k8s/install-tools.sh                 # install everything missing
#   scripts/k8s/install-tools.sh k3d kubectl      # only some tools
#   scripts/k8s/install-tools.sh check            # report installed vs. pinned versions, no download
#
# Checksum provenance (recorded here, not just asserted): kubectl, k3d, helm, kustomize, k6,
# kubeconform, cosign and kubeseal SHA-256s below were cross-checked against each project's own
# published checksum file at pin time (kubectl.sha256 / checksums.txt / helm's .sha256sum /
# CHECKSUMS / cosign_checksums.txt / sealed-secrets_0.41.2_checksums.txt — one HTTPS GET per
# tool, same release tag). kube-linter v0.8.3 publishes no plain checksum file for its Linux
# asset (only .sigstore.json bundles, which this cosign version could not verify against —
# "failed to verify log inclusion", most likely a bundle-format mismatch between an old release
# and a newer cosign, not evidence of tampering); its SHA-256 below is self-computed from the
# HTTPS download at pin time, the same provenance every other tool in
# scripts/prereqs/install.sh already relies on for assets with no published checksum file.
# Re-verify before bumping any version.
#
# Downloads use IPv4 only (see scripts/prereqs/install.sh header: IPv6 from WSL2 to some
# registries hangs). Idempotent: an installed version is left alone.
set -euo pipefail

PREFIX="${PREREQS_PREFIX:-$HOME/.local}"
OPT="$PREFIX/opt"
BIN="$PREFIX/bin"
mkdir -p "$OPT" "$BIN"
export PATH="$BIN:$PATH"

K3D_VERSION="5.9.0"
K3D_SHA256="06d8f25bc3a971c4eb29e0ff08429b180402db0f4dec838c9eac427e296800a0"

KUBECTL_VERSION="1.37.2"
KUBECTL_SHA256="c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4"

HELM_VERSION="4.3.2"
HELM_SHA256="d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983"

KUSTOMIZE_VERSION="5.8.3"
KUSTOMIZE_SHA256="cb9e31198d3f63b44848bd0afc4c8efc56e6cfef9c93a4a39a211940e6decd61"

K6_VERSION="2.4.0"
K6_SHA256="5a5ecf23529ccf93135bc7e5b0c1cd5126ecf5f2672e22eed1556a0ab74abb87"

KUBECONFORM_VERSION="0.8.0"
KUBECONFORM_SHA256="9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883"

KUBELINTER_VERSION="0.8.3"
KUBELINTER_SHA256="1a6d8419b11971372971fdbc22682b684ebfb7cf1c39591662d1b6ca736c41df"  # self-computed, see header

COSIGN_VERSION="3.1.4"
COSIGN_SHA256="424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa"

KUBESEAL_VERSION="0.41.2"
KUBESEAL_SHA256="666b5db6788ad1dedb56e2d97064e1cc3779aa77bbd6f4b56b62beeb1941ed79"

TMP="$(mktemp -d)"
STAGING="$OPT/.staging.$$"
trap 'rm -rf "$TMP" "$STAGING"' EXIT
rm -rf "$OPT"/.staging.*

log() { echo "[install-tools] $*"; }

# fetch <url> <sha256> <output>
fetch() {
  curl -4 -fsSL --retry 3 -o "$3" "$1" || return 1
  echo "$2  $3" | sha256sum -c --quiet -
}

stage() {
  mkdir -p "$STAGING/$1"
  echo "$STAGING/$1"
}

commit() {
  rm -rf "$2"
  mv -T "$1" "$2"
}

install_k3d() {
  local dir="$OPT/k3d-v$K3D_VERSION"
  if [ ! -x "$dir/k3d" ]; then
    log "k3d $K3D_VERSION"
    fetch "https://github.com/k3d-io/k3d/releases/download/v$K3D_VERSION/k3d-linux-amd64" \
      "$K3D_SHA256" "$TMP/k3d"
    local st; st="$(stage k3d)"
    install -m 755 "$TMP/k3d" "$st/k3d"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/k3d" "$BIN/k3d"
}

install_kubectl() {
  local dir="$OPT/kubectl-v$KUBECTL_VERSION"
  if [ ! -x "$dir/kubectl" ]; then
    log "kubectl $KUBECTL_VERSION"
    fetch "https://dl.k8s.io/release/v$KUBECTL_VERSION/bin/linux/amd64/kubectl" \
      "$KUBECTL_SHA256" "$TMP/kubectl"
    local st; st="$(stage kubectl)"
    install -m 755 "$TMP/kubectl" "$st/kubectl"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/kubectl" "$BIN/kubectl"
}

install_helm() {
  local dir="$OPT/helm-v$HELM_VERSION"
  if [ ! -x "$dir/helm" ]; then
    local tarball="helm-v$HELM_VERSION-linux-amd64.tar.gz" st
    log "helm $HELM_VERSION"
    fetch "https://get.helm.sh/$tarball" "$HELM_SHA256" "$TMP/$tarball"
    st="$(stage helm)"
    tar -xzf "$TMP/$tarball" -C "$st" --strip-components=1
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/helm" "$BIN/helm"
}

install_kustomize() {
  local dir="$OPT/kustomize-v$KUSTOMIZE_VERSION"
  if [ ! -x "$dir/kustomize" ]; then
    local tarball="kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" st
    log "kustomize $KUSTOMIZE_VERSION"
    fetch "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv$KUSTOMIZE_VERSION/$tarball" \
      "$KUSTOMIZE_SHA256" "$TMP/$tarball"
    st="$(stage kustomize)"
    tar -xzf "$TMP/$tarball" -C "$st"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/kustomize" "$BIN/kustomize"
}

install_k6() {
  local dir="$OPT/k6-v$K6_VERSION"
  if [ ! -x "$dir/k6" ]; then
    local tarball="k6-v$K6_VERSION-linux-amd64.tar.gz" st
    log "k6 $K6_VERSION"
    fetch "https://github.com/grafana/k6/releases/download/v$K6_VERSION/$tarball" \
      "$K6_SHA256" "$TMP/$tarball"
    st="$(stage k6)"
    tar -xzf "$TMP/$tarball" -C "$st" --strip-components=1
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/k6" "$BIN/k6"
}

install_kubeconform() {
  local dir="$OPT/kubeconform-v$KUBECONFORM_VERSION"
  if [ ! -x "$dir/kubeconform" ]; then
    local tarball="kubeconform-linux-amd64.tar.gz" st
    log "kubeconform $KUBECONFORM_VERSION"
    fetch "https://github.com/yannh/kubeconform/releases/download/v$KUBECONFORM_VERSION/$tarball" \
      "$KUBECONFORM_SHA256" "$TMP/$tarball"
    st="$(stage kubeconform)"
    tar -xzf "$TMP/$tarball" -C "$st"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/kubeconform" "$BIN/kubeconform"
}

install_kubelinter() {
  local dir="$OPT/kube-linter-v$KUBELINTER_VERSION"
  if [ ! -x "$dir/kube-linter" ]; then
    local tarball="kube-linter-linux.tar.gz" st
    log "kube-linter $KUBELINTER_VERSION"
    fetch "https://github.com/stackrox/kube-linter/releases/download/v$KUBELINTER_VERSION/$tarball" \
      "$KUBELINTER_SHA256" "$TMP/$tarball"
    st="$(stage kube-linter)"
    tar -xzf "$TMP/$tarball" -C "$st"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/kube-linter" "$BIN/kube-linter"
}

install_cosign() {
  local dir="$OPT/cosign-v$COSIGN_VERSION"
  if [ ! -x "$dir/cosign" ]; then
    log "cosign $COSIGN_VERSION"
    fetch "https://github.com/sigstore/cosign/releases/download/v$COSIGN_VERSION/cosign-linux-amd64" \
      "$COSIGN_SHA256" "$TMP/cosign"
    local st; st="$(stage cosign)"
    install -m 755 "$TMP/cosign" "$st/cosign"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/cosign" "$BIN/cosign"
}

install_kubeseal() {
  local dir="$OPT/kubeseal-v$KUBESEAL_VERSION"
  if [ ! -x "$dir/kubeseal" ]; then
    local tarball="kubeseal-$KUBESEAL_VERSION-linux-amd64.tar.gz" st
    log "kubeseal $KUBESEAL_VERSION"
    fetch "https://github.com/bitnami/sealed-secrets/releases/download/v$KUBESEAL_VERSION/$tarball" \
      "$KUBESEAL_SHA256" "$TMP/$tarball"
    st="$(stage kubeseal)"
    tar -xzf "$TMP/$tarball" -C "$st"
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/kubeseal" "$BIN/kubeseal"
}

check() {
  local ok=0
  check_one() { # check_one <name> <want> <have>
    if [ "$2" = "$3" ]; then
      printf 'OK  %-14s %s\n' "$1" "$3"
    else
      printf 'KO  %-14s want %s, have %s\n' "$1" "$2" "${3:-missing}"
      ok=1
    fi
  }
  check_one k3d "$K3D_VERSION" "$(k3d version 2>/dev/null | awk '/^k3d version/ {print $3}' | tr -d v)"
  check_one kubectl "$KUBECTL_VERSION" "$(kubectl version --client 2>/dev/null | grep -o 'v[0-9.]*' | head -1 | tr -d v)"
  check_one helm "$HELM_VERSION" "$(helm version --short 2>/dev/null | grep -o 'v[0-9.]*' | head -1 | tr -d v)"
  check_one kustomize "$KUSTOMIZE_VERSION" "$(kustomize version 2>/dev/null | grep -o 'v[0-9.]*' | head -1 | tr -d v)"
  check_one k6 "$K6_VERSION" "$(k6 version 2>/dev/null | grep -o 'v[0-9.]*' | head -1 | tr -d v)"
  check_one kubeconform "$KUBECONFORM_VERSION" "$(kubeconform -v 2>/dev/null | awk '{print $1}' | tr -d v)"
  check_one kube-linter "$KUBELINTER_VERSION" "$(kube-linter version 2>/dev/null | tr -d v)"
  check_one cosign "$COSIGN_VERSION" "$(cosign version 2>/dev/null | awk '/GitVersion/ {print $2}' | tr -d v)"
  check_one kubeseal "$KUBESEAL_VERSION" "$(kubeseal --version 2>&1 | grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+' | head -1)"
  return "$ok"
}

main() {
  if [ "${1:-}" = "check" ]; then
    check
    return $?
  fi
  local tools=("$@")
  if [ ${#tools[@]} -eq 0 ]; then
    tools=(k3d kubectl helm kustomize k6 kubeconform kubelinter cosign kubeseal)
  fi
  local t
  for t in "${tools[@]}"; do
    case "$t" in
      k3d) install_k3d ;;
      kubectl) install_kubectl ;;
      helm) install_helm ;;
      kustomize) install_kustomize ;;
      k6) install_k6 ;;
      kubeconform) install_kubeconform ;;
      kube-linter | kubelinter) install_kubelinter ;;
      cosign) install_cosign ;;
      kubeseal) install_kubeseal ;;
      *) echo "unknown tool: $t" >&2; exit 1 ;;
    esac
  done
  check
}

main "$@"
