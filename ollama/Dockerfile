# SecRAG custom Ollama image (T11.4.3): bakes the embedding model (`bge-m3`) into the image
# at build time, so a fresh container embeds without ever pulling it — fixing the Azure
# `secrag-ollama` ephemeral-storage loss (ADR phase 11, runbook gotcha 4: the model used to
# disappear on every scale-to-zero / replica restart and had to be pulled by hand). Build
# context: ./ollama (no other files needed).
#
# Base pinned by digest, not `:latest` (today Azure runs `docker.io/ollama/ollama:latest`,
# which floats): ollama/ollama 0.35.1 (the newest stable release at the time this was pinned;
# `latest` on Docker Hub still pointed at 0.35.0 — see ADR phase 11 decision 8 for how the
# digest was found and verified: `docker pull`, then `ollama --version` inside the pulled
# image). A base-image bump is a deliberate, reviewed change, never silent.
FROM ollama/ollama@sha256:292ee7945dfc3d5840a181f3ab86fedb1e66703e02c8af98b50f4da56b7e278c

# Must match `Settings.embed_model` (backend/src/rag_app/config.py) — the backend only ever
# asks Ollama for this exact name; test_ollama_image.py guards the two against drift. Ollama's
# registry does not let `ollama pull` a model by a manifest digest the way `docker pull`
# does for OCI images (digests are reported only AFTER a pull, by `ollama list`/`/api/tags`),
# so the model itself is pinned by NAME here (`bge-m3`, i.e. `bge-m3:latest` — the only tag
# ever published for it) and its digest is RECORDED (not enforced at build time) by running
# `python -m rag_app.devtools.ollama_digest` against a throwaway container after the build —
# this is the SAME digest the gate's seed staleness check already records
# (`7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`, confirmed identical
# from a fresh `--network none` build of this image — see ADR phase 11 decision 8 and
# README). A silent upstream change under the same tag would change this recorded digest.
ARG EMBED_MODEL=bge-m3

# Start the server, wait for it to answer, pull the model into this layer's filesystem, then
# stop the server — the standard pattern for baking an Ollama model into an image (there is
# no "download without a running server" mode). Everything happens inside ONE RUN step so the
# pulled model blobs are committed to this layer.
RUN ollama serve & \
    for _ in $(seq 1 60); do \
      ollama list >/dev/null 2>&1 && break; \
      sleep 1; \
    done && \
    ollama list >/dev/null 2>&1 || { echo "ollama server never became ready" >&2; exit 1; } && \
    ollama pull "$EMBED_MODEL" && \
    ollama list | grep -q "$EMBED_MODEL" && \
    pkill -f "ollama serve" && \
    sleep 1

EXPOSE 11434
