# CloudNativePG + pgvector (T16.1.4, resolves TF32 -- 16-A's own pre-check finding: "CNPG must
# run PG16 + pgvector -> standard image or small custom image"). CNPG's own official postgresql
# image has no pgvector baked in; this builds it FROM that exact pinned base (same image the
# CNPG operator itself knows how to run: entrypoint, extensions dir, postgres UID/GID), adding
# ONLY the pgvector extension, compiled from source in a throwaway builder stage.
#
# Base pinned by digest (X1): ghcr.io/cloudnative-pg/postgresql:16.10-bookworm, confirmed via
# `docker buildx imagetools inspect` at pin time. pgvector pinned to its latest tagged release
# (v0.8.7, github.com/pgvector/pgvector/tags) -- built from source rather than an apt package
# because PGDG's own apt repo does not reliably ship a postgresql-16-pgvector package for every
# Debian codename/architecture combination; building from the base image's own
# postgresql-server-dev-16 (already present via the base image's PGDG apt source) guarantees a
# binary built against the EXACT server headers this image runs, which an externally-built .deb
# cannot guarantee.
#
# Built + pushed into the k3d-managed local registry by scripts/k8s/cluster-up.sh (never on
# Azure -- Phase 16 is local-only); the resulting LOCAL tag is referenced by
# deploy/k8s/base/cnpg/cluster.yaml. A locally built image has no meaningful upstream digest to
# pin ahead of time (unlike a published registry image) -- reproducibility instead comes from
# this Dockerfile's own pinned FROM digest + pinned pgvector tag, not from pinning the resulting
# local image's digest.
FROM ghcr.io/cloudnative-pg/postgresql@sha256:bf0b0ec764b26fbb9136476ed29ad92abdc1277d0465194b8bf420f0116b974d AS builder
USER root
ARG PGVECTOR_VERSION=v0.8.7
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential git ca-certificates postgresql-server-dev-16; \
    git clone --branch "$PGVECTOR_VERSION" --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector; \
    cd /tmp/pgvector; \
    make OPTFLAGS=""; \
    make install; \
    apt-get purge -y --auto-remove build-essential git postgresql-server-dev-16; \
    rm -rf /var/lib/apt/lists/* /tmp/pgvector

# Fresh layer from the SAME pinned base (not FROM builder): keeps the final image's default
# user/entrypoint/metadata exactly as CNPG expects, picking up only the compiled extension files
# from the builder stage above.
FROM ghcr.io/cloudnative-pg/postgresql@sha256:bf0b0ec764b26fbb9136476ed29ad92abdc1277d0465194b8bf420f0116b974d
COPY --from=builder /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/vector.so
COPY --from=builder /usr/share/postgresql/16/extension/vector.control /usr/share/postgresql/16/extension/vector.control
COPY --from=builder /usr/share/postgresql/16/extension/vector--*.sql /usr/share/postgresql/16/extension/
