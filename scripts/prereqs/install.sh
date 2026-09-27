#!/usr/bin/env bash
# Install the local toolchain for Phase 11+ as user binaries (no sudo), under
# ~/.local/opt/<tool>-<version> with entry points in ~/.local/bin.
#
#   scripts/prereqs/install.sh              # install everything missing
#   scripts/prereqs/install.sh node pg age  # only some tools
#   scripts/prereqs/install.sh --link-az    # make the Linux Azure CLI the default `az`
#   scripts/prereqs/install.sh --lock-az    # regenerate azure-cli.lock.txt (maintainers)
#
# Supply chain: every download is pinned and verified with SHA-256; npm packages are
# verified by npm's registry integrity check; the Azure CLI and its ~150 Python
# dependencies install from the committed hashed lock file azure-cli.lock.txt
# (`uv pip install --require-hashes`) into a venv on a pinned uv-managed Python.
#
# Bumping the Azure CLI (deliberate, reviewed change):
#   1. set AZ_VERSION below and the pin in scripts/prereqs/azure-cli.in;
#   2. run scripts/prereqs/install.sh --lock-az (re-resolves every dependency);
#   3. review the lock diff, commit both files, run scripts/prereqs/install.sh az.
# The venv directory name carries a digest of the lock, so a new lock gets a fresh venv.
#
# Atomic installs: archives are unpacked into a staging directory next to the target
# and renamed into place, so an interrupted run is never taken for a finished install.
# The Azure CLI venv cannot be moved (absolute paths); it is built in place and marked
# complete last — a venv without the marker is removed and rebuilt.
#
# Downloads use IPv4 only: IPv6 from WSL2 to some registries hangs. Idempotent: an
# installed version is left alone. Check the result with scripts/prereqs/check.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PREREQS_PREFIX lets a test install into a scratch directory instead of ~/.local.
PREFIX="${PREREQS_PREFIX:-$HOME/.local}"
OPT="$PREFIX/opt"
BIN="$PREFIX/bin"
mkdir -p "$OPT" "$BIN"
export PATH="$HOME/.local/bin:$PATH"

NODE_VERSION="22.23.3"
NODE_SHA256="df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de"
NPM_VERSION="11.20.0"

# PostgreSQL 16 client from the PGDG archive (Ubuntu 26.04 "resolute" build) and the
# libpq it needs; unpacked with dpkg-deb, never installed system-wide. PGDG drops
# superseded builds from the live pool; they stay (same files, same SHA-256) under
# apt-archive.postgresql.org with the same pool layout, which is the fallback.
PGDG_POOL="${PGDG_POOL:-https://apt.postgresql.org/pub/repos/apt/pool/main/p}"
PGDG_ARCHIVE_POOL="https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p"
PG_CLIENT_DEB="postgresql-client-16_16.15-1.pgdg26.04+2_amd64.deb"
PG_CLIENT_SHA256="52fd38615ce65bd9b0442a21e611986e0cc7b87e1143e2464668be17fcb77314"
LIBPQ_DEB="libpq5_18.6-1.pgdg26.04+2_amd64.deb"
LIBPQ_SHA256="a56bfe4843ae432e7338d0139190397db69a343412e489ae637bfb39c2d6fc30"
PG_DIR="$OPT/pg-client-16.15"

AGE_VERSION="1.3.2"
AGE_SHA256="cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"

SHELLCHECK_VERSION="0.11.0"
SHELLCHECK_SHA256="8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198"

GH_VERSION="2.101.0"
GH_SHA256="9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8"

AZ_VERSION="2.90.0"
AZ_PYTHON="3.12.14"   # exact uv-managed CPython for the venv
AZ_IN="$SCRIPT_DIR/azure-cli.in"
AZ_LOCK="$SCRIPT_DIR/azure-cli.lock.txt"
AZ_LOCK_ID="$(sha256sum "$AZ_LOCK" | cut -c1-12)"
AZ_DIR="$OPT/azure-cli-$AZ_VERSION-$AZ_LOCK_ID"
AZ_DONE=".secrag-install-complete"
# The Linux CLI keeps its own config/token cache here, never in the Windows profile
# that ~/.azure may point to (a symlink into /mnt/c).
# shellcheck disable=SC2016  # expanded by the wrapper at run time
AZ_CONFIG_DEFAULT='$HOME/.azure-linux'

TMP="$(mktemp -d)"
STAGING="$OPT/.staging.$$"
trap 'rm -rf "$TMP" "$STAGING"' EXIT
# Staging left behind by a killed run (SIGKILL skips the trap) is never used again.
rm -rf "$OPT"/.staging.*

log() { echo "[install] $*"; }

# fetch <url> <sha256> <output>
fetch() {
  curl -4 -fsSL --retry 3 -o "$3" "$1" || return 1
  echo "$2  $3" | sha256sum -c --quiet -
}

# stage <name>: print a fresh staging directory on the same filesystem as $OPT.
stage() {
  mkdir -p "$STAGING/$1"
  echo "$STAGING/$1"
}

# commit <staged dir> <final dir>: replace any partial leftover, then rename (atomic).
commit() {
  rm -rf "$2"
  mv -T "$1" "$2"
}

# write_exec <path>: write stdin to an executable file atomically (never through a
# symlink that may already be at <path>).
write_exec() {
  local tmp
  tmp="$(mktemp "$(dirname "$1")/.$(basename "$1").XXXXXX")"
  cat > "$tmp"
  chmod 755 "$tmp"
  mv -f "$tmp" "$1"
}

install_node() {
  local dir="$OPT/node-v$NODE_VERSION"
  # Installed = node present AND the pinned npm answering; anything else (including
  # a directory left by an older, non-atomic install) is rebuilt from scratch.
  if [ ! -x "$dir/bin/node" ] \
     || [ "$(PATH="$dir/bin:$PATH" npm --version 2>/dev/null)" != "$NPM_VERSION" ]; then
    local tarball="node-v$NODE_VERSION-linux-x64.tar.xz" st
    log "node $NODE_VERSION + npm $NPM_VERSION"
    fetch "https://nodejs.org/dist/v$NODE_VERSION/$tarball" "$NODE_SHA256" "$TMP/$tarball"
    st="$(stage node)"
    tar -xJf "$TMP/$tarball" -C "$st" --strip-components=1
    # npm's shebang resolves `node` through PATH and npm derives its global prefix
    # from that node: with the staged node first, npm 11 lands in the staged tree
    # (its bin links are relative, so the tree can be renamed afterwards).
    PATH="$st/bin:$PATH" NODE_OPTIONS="--dns-result-order=ipv4first" \
      npm install -g --no-fund --no-audit --loglevel=error "npm@$NPM_VERSION"
    commit "$st" "$dir"
  fi
  # Replace any older user-level node/npm entry points (a previous install left a
  # plain `node` binary and npm symlinks into ~/.local/lib/node_modules).
  local tool
  for tool in node npm npx corepack; do
    ln -sfn "$dir/bin/$tool" "$BIN/$tool"
  done
}

# fetch_pgdg <package dir> <deb> <sha256> <output>: live pool first, archive on failure.
fetch_pgdg() {
  if ! fetch "$PGDG_POOL/$1/$2" "$3" "$4" 2>/dev/null; then
    log "$2 not in the live PGDG pool; trying apt-archive.postgresql.org"
    fetch "$PGDG_ARCHIVE_POOL/$1/$2" "$3" "$4"
  fi
}

install_pg() {
  if [ ! -x "$PG_DIR/usr/lib/postgresql/16/bin/pg_dump" ]; then
    local st
    log "PostgreSQL 16 client (PGDG)"
    fetch_pgdg postgresql-16 "$PG_CLIENT_DEB" "$PG_CLIENT_SHA256" "$TMP/client.deb"
    fetch_pgdg postgresql-18 "$LIBPQ_DEB" "$LIBPQ_SHA256" "$TMP/libpq.deb"
    st="$(stage pg)"
    dpkg-deb -x "$TMP/client.deb" "$st"
    dpkg-deb -x "$TMP/libpq.deb" "$st"
    commit "$st" "$PG_DIR"
  fi
  local tool
  for tool in psql pg_dump pg_restore pg_dumpall pg_isready; do
    write_exec "$BIN/$tool" <<EOF
#!/usr/bin/env bash
# Generated by scripts/prereqs/install.sh: PostgreSQL 16 client (user install).
export LD_LIBRARY_PATH="$PG_DIR/usr/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$PG_DIR/usr/lib/postgresql/16/bin/$tool" "\$@"
EOF
  done
}

install_age() {
  local dir="$OPT/age-v$AGE_VERSION"
  if [ ! -x "$dir/age" ] || [ ! -x "$dir/age-keygen" ]; then
    local tarball="age-v$AGE_VERSION-linux-amd64.tar.gz" st
    log "age $AGE_VERSION"
    fetch "https://github.com/FiloSottile/age/releases/download/v$AGE_VERSION/$tarball" \
      "$AGE_SHA256" "$TMP/$tarball"
    st="$(stage age)"
    tar -xzf "$TMP/$tarball" -C "$st" --strip-components=1
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/age" "$BIN/age"
  ln -sfn "$dir/age-keygen" "$BIN/age-keygen"
}

install_shellcheck() {
  local dir="$OPT/shellcheck-v$SHELLCHECK_VERSION"
  if [ ! -x "$dir/shellcheck" ]; then
    local tarball="shellcheck-v$SHELLCHECK_VERSION.linux.x86_64.tar.xz" st
    log "shellcheck $SHELLCHECK_VERSION"
    fetch "https://github.com/koalaman/shellcheck/releases/download/v$SHELLCHECK_VERSION/$tarball" \
      "$SHELLCHECK_SHA256" "$TMP/$tarball"
    st="$(stage shellcheck)"
    tar -xJf "$TMP/$tarball" -C "$st" --strip-components=1
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/shellcheck" "$BIN/shellcheck"
}

install_gh() {
  local dir="$OPT/gh-$GH_VERSION"
  if [ ! -x "$dir/bin/gh" ]; then
    local tarball="gh_${GH_VERSION}_linux_amd64.tar.gz" st
    log "gh $GH_VERSION"
    fetch "https://github.com/cli/cli/releases/download/v$GH_VERSION/$tarball" \
      "$GH_SHA256" "$TMP/$tarball"
    st="$(stage gh)"
    tar -xzf "$TMP/$tarball" -C "$st" --strip-components=1
    commit "$st" "$dir"
  fi
  ln -sfn "$dir/bin/gh" "$BIN/gh"
}

# az_wrapper <path>: an entry point that pins the CLI's config dir away from the
# Windows profile and refuses a config dir under /mnt (Windows drives).
az_wrapper() {
  write_exec "$1" <<EOF
#!/usr/bin/env bash
# Generated by scripts/prereqs/install.sh: Linux Azure CLI $AZ_VERSION (user venv).
# SECRAG_AZ_WRAPPER — own config dir, never the Windows profile behind ~/.azure.
export AZURE_CONFIG_DIR="\${AZURE_CONFIG_DIR:-$AZ_CONFIG_DEFAULT}"
case "\$(readlink -m "\$AZURE_CONFIG_DIR")" in
  /mnt/*) echo "az: AZURE_CONFIG_DIR resolves under /mnt (a Windows profile); refusing" >&2; exit 1 ;;
esac
[ -d "\$AZURE_CONFIG_DIR" ] || mkdir -m 700 -p "\$AZURE_CONFIG_DIR"
exec "$AZ_DIR/bin/az" "\$@"
EOF
}

# Linux Azure CLI in its own venv (pinned uv-managed Python, hashed lock). Exposed
# as `az-linux` so it does not shadow the Windows `az` until `--link-az`.
install_az() {
  grep -qx "azure-cli==$AZ_VERSION.*" "$AZ_LOCK" \
    || { echo "azure-cli.lock.txt does not pin azure-cli==$AZ_VERSION (run --lock-az)" >&2; return 1; }
  if [ ! -f "$AZ_DIR/$AZ_DONE" ]; then
    command -v uv >/dev/null || { echo "uv is required for the Azure CLI venv" >&2; return 1; }
    log "azure-cli $AZ_VERSION (venv, Python $AZ_PYTHON, lock $AZ_LOCK_ID)"
    rm -rf "$AZ_DIR"
    uv venv -q --managed-python --python "$AZ_PYTHON" "$AZ_DIR"
    uv pip install -q --require-hashes --python "$AZ_DIR/bin/python" -r "$AZ_LOCK"
    "$AZ_DIR/bin/python" --version | grep -qx "Python $AZ_PYTHON"
    touch "$AZ_DIR/$AZ_DONE"
  fi
  [ -d "$HOME/.azure-linux" ] || mkdir -m 700 "$HOME/.azure-linux"
  chmod 700 "$HOME/.azure-linux"
  az_wrapper "$BIN/az-linux"
}

link_az() {
  [ -f "$AZ_DIR/$AZ_DONE" ] || { echo "install the Linux Azure CLI first: $0 az" >&2; return 1; }
  az_wrapper "$BIN/az"
  log "\`az\` in WSL now runs the Linux Azure CLI ($AZ_VERSION), config dir ~/.azure-linux"
  log "next step (user, in WSL): az login --use-device-code"
}

lock_az() {
  command -v uv >/dev/null || { echo "uv is required" >&2; return 1; }
  grep -qx "azure-cli==$AZ_VERSION" "$AZ_IN" \
    || { echo "azure-cli.in must pin azure-cli==$AZ_VERSION" >&2; return 1; }
  uv pip compile -q --upgrade --generate-hashes \
    --python-version "${AZ_PYTHON%.*}" --python-platform x86_64-unknown-linux-gnu \
    --custom-compile-command "scripts/prereqs/install.sh --lock-az" \
    "$AZ_IN" -o "$AZ_LOCK"
  log "wrote $AZ_LOCK — review the diff before committing"
}

main() {
  case "${1:-}" in
    --link-az) link_az; return ;;
    --lock-az) lock_az; return ;;
  esac
  local tools=("$@")
  [ ${#tools[@]} -eq 0 ] && tools=(node pg age shellcheck gh az)
  local t
  for t in "${tools[@]}"; do
    case "$t" in
      node) install_node ;;
      pg) install_pg ;;
      age) install_age ;;
      shellcheck) install_shellcheck ;;
      gh) install_gh ;;
      az) install_az ;;
      *) echo "unknown tool: $t (node pg age shellcheck gh az)" >&2; exit 2 ;;
    esac
  done
  log "done — run scripts/prereqs/check.sh"
}

main "$@"
