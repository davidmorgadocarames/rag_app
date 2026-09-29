#!/usr/bin/env bash
# Create the development database volume ONCE (T11.2.1).
#
#   scripts/dev/create_dev_volume.sh
#
# docker-compose.yml declares `pgdata` as the EXTERNAL volume `rag_ia_pgdata`: compose never
# creates, recreates or removes it (not even `down -v`), and `up` fails clearly when it is
# missing. This script is the only thing that creates it:
#   - volume exists → prints its creation time and does NOTHING (never recreated, never
#     emptied, never relabelled — the developer's data lives there);
#   - volume missing → creates an empty one (a fresh clone on a new machine).
# It also reports a stray `rag_app_pgdata` (the pre-T11.2.1 project-derived volume) without
# touching it: removing it is a manual, verified step (see the ADR phase 11, T11.2.1).
set -euo pipefail

VOLUME="${SECRAG_DEV_VOLUME:-rag_ia_pgdata}"   # override for tests only
STRAY="${SECRAG_STRAY_VOLUME:-rag_app_pgdata}"

command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker is not reachable (start Docker Desktop)" >&2; exit 2; }

if created="$(docker volume inspect -f '{{.CreatedAt}}' "$VOLUME" 2>/dev/null)"; then
  echo "dev volume $VOLUME exists (created $created) — nothing to do"
else
  docker volume create --label secrag.volume=dev-db "$VOLUME" >/dev/null
  echo "dev volume $VOLUME created (empty: the first 'docker compose up' initialises it)"
fi

if docker volume inspect "$STRAY" >/dev/null 2>&1; then
  echo "note: stray volume $STRAY (pre-T11.2.1, project-derived) still exists; compose no longer"
  echo "      uses it. It is removed only by hand after verification (ADR phase 11, T11.2.1)."
fi
