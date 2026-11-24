#!/usr/bin/env bash
# Back up / restore the Bitnami Sealed Secrets controller's active key (T16.0.3), age-encrypted,
# outside git. The controller itself is installed later (T16.1.3) — this script only moves the
# `kubernetes.io/tls` Secret it owns in `kube-system`, so a cluster recreated with `cluster-up.sh`
# keeps unsealing secrets that were sealed against the previous instance.
#
# Usage:
#   scripts/k8s/sealed-secrets-key.sh backup  --recipient <age1…public key> [--out FILE]
#   scripts/k8s/sealed-secrets-key.sh restore --identity  <private key file> [--in FILE]
#
# backup is a no-op (exit 0, logged) when the controller/secret does not exist yet — the normal
# case before T16.1.3. restore is a no-op (exit 0, logged) when no backup file exists yet.
# The identity file goes through the same safety checks as scripts/db/restore.sh's --identity:
# a regular file (no symlink), owned by you, mode 600/400, never inside a git work tree. The key
# NEVER enters git or any command's argv/stdout — only `age`'s own file-based en/decryption.
set -euo pipefail

NS="kube-system"
LABEL="sealedsecrets.bitnami.com/sealed-secrets-key=active"
DEFAULT_FILE="$HOME/.secrag-k8s/sealed-secrets-key.age"

die() { echo "sealed-secrets-key.sh: $*" >&2; exit 1; }
log() { echo "[sealed-secrets-key] $*"; }

inside_git_work_tree() {
  git -C "$(dirname "$1")" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

usage() {
  echo "usage: sealed-secrets-key.sh backup --recipient <age1…> [--out FILE]" >&2
  echo "       sealed-secrets-key.sh restore --identity <file> [--in FILE]" >&2
  exit 2
}

cmd="${1:-}"
[ -n "$cmd" ] || usage
shift

case "$cmd" in
  backup)
    recipient="" file="$DEFAULT_FILE"
    while [ $# -gt 0 ]; do
      case "$1" in
        --recipient) shift; recipient="${1:-}" ;;
        --out) shift; file="${1:-}" ;;
        *) usage ;;
      esac
      shift
    done
    [ -n "$recipient" ] || die "no age recipient: pass --recipient <age1…public key>"
    secret_name="$(kubectl -n "$NS" get secret -l "$LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [ -z "$secret_name" ]; then
      log "no active sealed-secrets key Secret in $NS yet (controller not installed) — skipping backup"
      exit 0
    fi
    mkdir -p "$(dirname "$file")"
    chmod 700 "$(dirname "$file")"
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    kubectl -n "$NS" get secret "$secret_name" -o yaml > "$tmp"
    age -r "$recipient" -o "$file.tmp" "$tmp"
    mv -f "$file.tmp" "$file"
    chmod 600 "$file"
    log "backed up Secret $NS/$secret_name -> $file"
    ;;

  restore)
    identity="" file="$DEFAULT_FILE"
    while [ $# -gt 0 ]; do
      case "$1" in
        --identity) shift; identity="${1:-}" ;;
        --in) shift; file="${1:-}" ;;
        *) usage ;;
      esac
      shift
    done
    [ -n "$identity" ] || die "no identity: pass --identity <private key file>"
    if [ ! -f "$file" ]; then
      log "no backup at $file yet — skipping restore"
      exit 0
    fi
    [ -f "$identity" ] && [ ! -L "$identity" ] || die "the identity must be a regular file (no symlink)"
    [ "$(stat -c %u "$identity")" = "$(id -u)" ] || die "the identity file is not owned by you"
    case "$(stat -c %a "$identity")" in
      600 | 400) ;;
      *) die "the identity file must be mode 600 or 400 (it is the PRIVATE key)" ;;
    esac
    if inside_git_work_tree "$identity"; then
      die "refusing an identity inside a git work tree — the private key never goes near a repository"
    fi
    kubectl get namespace "$NS" >/dev/null 2>&1 || die "namespace $NS not found — is the cluster up?"
    age -d -i "$identity" -- "$file" | kubectl apply -f -
    log "restored sealed-secrets key Secret from $file into $NS"
    ;;

  *) usage ;;
esac
