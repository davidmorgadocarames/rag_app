# SecRAG custom Ollama image (T11.4.3 + T11.6b.6b "slim"): bakes the embedding model
# (`bge-m3`) into the image at build time, so a fresh container embeds without ever pulling
# it — fixing the Azure `secrag-ollama` ephemeral-storage loss (ADR phase 11, runbook
# gotcha 4). Build context: ./ollama (no other files needed).
#
# Two stages:
#   builder — the upstream fat Ollama image (GPU backends + CPU backends + Go server),
#             used only to pull `bge-m3` into its model store (same recipe as before T11.6b).
#   final   — CPU-only runtime: `ubuntu:24.04` pinned by digest, which is the EXACT base
#             image the upstream `ollama/ollama` release itself is built on (confirmed
#             `/etc/os-release` = Ubuntu 24.04.5 LTS, `ldd` glibc 2.39 on both — see ADR
#             phase 11 decision 8 / block H), so the copied `/bin/ollama` binary and its
#             CPU-backend `.so`s need no ABI shim and no Debian/distroless
#             `GLIBC_x not found` risk. Drops `cuda_v12`, `cuda_v13`, `mlx_cuda_v13` and
#             `vulkan` under `/usr/lib/ollama` — GPU-only backends this CPU-only Azure
#             Ollama instance (no GPU attached) never loads, together 5.08 of the fat
#             image's 5.12 GB of `/usr/lib/ollama` (ADR block H measured it at build time).
#             Every `libggml-cpu-*.so` variant is kept (one process picks the right one for
#             the host CPU's feature set at runtime — `cpu`, `sse42`, `x64`, `sandybridge`,
#             `ivybridge`, `haswell`, `piledriver`, `alderlake`, `cannonlake`, `cascadelake`,
#             `cooperlake`, `icelake`, `sapphirerapids`, `skylakex`, `zen4`, whichever ship
#             upstream — never hard-code the list of names, only the glob + the drop list).
#
# Base pinned by digest, not `:latest` (today Azure runs `docker.io/ollama/ollama:latest`,
# which floats): ollama/ollama 0.35.1 (the newest stable release at the time this was pinned;
# `latest` on Docker Hub still pointed at 0.35.0 — see ADR phase 11 decision 8 for how the
# digest was found and verified: `docker pull`, then `ollama --version` inside the pulled
# image). A base-image bump is a deliberate, reviewed change, never silent.
FROM ollama/ollama@sha256:292ee7945dfc3d5840a181f3ab86fedb1e66703e02c8af98b50f4da56b7e278c AS builder

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
# README; the slim final stage was re-checked against the same digest in block H). A silent
# upstream change under the same tag would change this recorded digest.
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

# --- final: CPU-only runtime, same Ubuntu 24.04 base as upstream, ~1.2-1.4 GB from 6.3 GB ---
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS final

# ca-certificates: the backend/eval/registry calls Ollama over plain HTTP on the compose/
# Azure internal network (no TLS needed there), but `ollama pull`/`ollama push` and the
# upstream registry client use HTTPS — kept for parity with the fat image's behaviour and
# because a bare `ubuntu:24.04` ships no CA bundle at all.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /bin/ollama /bin/ollama

# CPU-only backend libraries: every `libggml-cpu-*.so` variant, the shared
# base/ggml/llama/mtmd libs, `libgomp` (OpenMP, needed by the CPU backend), the CPU-only
# CLI helpers, and the upstream licence/notice files — never `cuda_v12`, `cuda_v13`,
# `mlx_cuda_v13` or `vulkan` (GPU-only, dropped above). No NVIDIA env vars either (the fat
# image's `NVIDIA_VISIBLE_DEVICES=all` / `NVIDIA_DRIVER_CAPABILITIES` are meaningless here).
COPY --from=builder /usr/lib/ollama/libggml-cpu-*.so /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libggml-base.so* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libggml.so* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libgomp.so* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libllama.so* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libllama-common.so* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libllama-quantize-impl.so /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libllama-server-impl.so /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/libmtmd.so* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/llama-quantize /usr/lib/ollama/llama-quantize
COPY --from=builder /usr/lib/ollama/llama-server /usr/lib/ollama/llama-server
COPY --from=builder /usr/lib/ollama/*LICENSE* /usr/lib/ollama/
COPY --from=builder /usr/lib/ollama/*NOTICE* /usr/lib/ollama/

# The model store: `bge-m3`'s blobs, pulled by the builder stage above (and its keypair/
# cache dir — the server creates these on first run; copying them from the builder avoids a
# first-run re-creation and keeps the exact same server identity key as a bare `ollama pull`
# would have produced).
COPY --from=builder /root/.ollama /root/.ollama

ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
