# ADR phase 16 — Kubernetes on k3d

- **Status:** **16-A (toolchain + local cluster, T16.0.1–T16.0.6) code-complete** on
  `phase-16-kubernetes`, not yet promoted. This ADR records decisions as each block of Phase 16
  lands; it will gain further sections (manifests, health/rollouts, autoscaling, cluster
  security, observability) as blocks 16-B…16-F complete.
- **Related:** the private Phase 16 planning record (P16-D1…D4, TF32–TF37),
  [ADR phase 11](adr_phase11_stability.md) (T11.0.12's native-Ollama-from-container pattern,
  reused here for native-Ollama-from-pod). Phase 15's ADR (signed images / restricted role,
  consumed from T16.4.4 on) has not landed yet at this branch's base — link added once it exists.

## Context

Phase 16 runs the whole app on a local multi-node Kubernetes cluster (k3d) with production
practices — health probes, zero-downtime rollouts, autoscaling, NetworkPolicies, signed images —
while keeping an AKS overlay rendered-and-validated-only (never deployed; cost). Everything here
is local: no Azure cost, no production exposure.

## Decisions

### 16-A — Toolchain + local cluster (T16.0.1–T16.0.6)

**Toolchain (T16.0.2).** `scripts/k8s/install-tools.sh` pins k3d 5.9.0, kubectl 1.37.2, helm
4.3.2, kustomize 5.8.3, k6 2.4.0, kubeconform 0.8.0, kube-linter 0.8.3 and cosign 3.1.4, installed
as user binaries under `~/.local/opt/<tool>-<version>` with `~/.local/bin` symlinks — same
atomic stage-then-rename pattern as `scripts/prereqs/install.sh`. Every SHA-256 was cross-checked
against the project's own published checksum file (`kubectl.sha256`, k3d's `checksums.txt`,
helm's `.sha256sum`, kustomize's `checksums.txt`, k6's `*-checksums.txt`, kubeconform's
`CHECKSUMS`, cosign's `cosign_checksums.txt`) at pin time, **except kube-linter**: v0.8.3
publishes no plain checksum file for its Linux asset, only `.sigstore.json` keyless-signature
bundles, and `cosign verify-blob --bundle … ` against that bundle failed with "failed to verify
log inclusion: transparency log certificate does not match" (most likely a bundle-format
mismatch between that release and cosign 3.1.4, not an actual tampering signal — the binary's
SHA-256 was still recomputed directly from the HTTPS download, the same provenance level
`scripts/prereqs/install.sh` already relies on for several of its own pins that have no
published checksum file either). Re-verify kube-linter's pin independently before any future
version bump.

**Cluster (T16.0.3).** `scripts/k8s/cluster-up.sh` / `cluster-down.sh` create a k3d cluster named
`secrag`: 1 server + 2 agents, Traefik disabled (`--k3s-arg --disable=traefik@server:0` — Envoy
Gateway takes over at T16.1.1), a k3d-managed local image registry (`secrag-registry`, port
5050), and Docker memory limits per node (`--servers-memory 2g --agents-memory 1g`). Both scripts
are idempotent (`cluster-up.sh` starts an existing cluster rather than recreating it;
`cluster-down.sh` is a no-op if the cluster does not exist). The Sealed Secrets controller's
active key (added at T16.1.3) is backed up/restored outside git via `age`
(`scripts/k8s/sealed-secrets-key.sh`, default path `~/.secrag-k8s/sealed-secrets-key.age`,
0600, never a git work tree) — both subcommands are no-ops today (the controller does not exist
yet) and the round-trip is proven with a disposable Secret + disposable age keypair in
`scripts/k8s/smoke-16-0.sh`, never a real key.

**Native Ollama from pods (T16.0.4).** Reuses the T11.0.12 Docker-Compose pattern: no Ollama
container runs in the cluster. A selector-less Service `ollama-host` in the `secrag` namespace
(`deploy/k8s/base/ollama/service.yaml`) is paired with a hand-managed `EndpointSlice`
(`endpointslice.yaml.tmpl`, rendered with `envsubst` by `cluster-up.sh`) pointing at the host's
resolved IP. The host IP is resolved by `docker exec`-ing into the k3d server node container and
running `nslookup host.docker.internal` there (the k3s node image ships no `getent`), on every
`cluster-up.sh` run — container/VM IPs are not stable across a teardown/recreate cycle. Proven
end-to-end with a real pod (`smoke-16-0.sh`'s `ollama-reachable-from-pod`: a busybox pod
`wget`s `http://ollama-host.secrag.svc.cluster.local:11434/api/version` and gets a real answer).
A templated `NetworkPolicy` (`networkpolicy.yaml.tmpl`) restricts the *intended* backend egress
to that IP:port once T16.1.1 labels a backend Deployment `app.kubernetes.io/name: backend`.

**Self-reported boundary 1 — Ollama port collision on the dev machine.** The connectivity
*mechanism* above is proven correct, but on the machine this block ran on, a separately
installed Windows-native Ollama app
(`C:\Users\Gatrix\AppData\Local\Programs\Ollama\ollama.exe`, confirmed via
`Get-NetTCPConnection -LocalPort 11434` → PID → `Get-Process`) already owns Windows'
`127.0.0.1:11434` ahead of the WSL-native `ollama serve` the project is built around — so
`host.docker.internal` (Docker Desktop → Windows loopback) reaches the Windows app's Ollama
(version 0.40.17 at the time) instead of the WSL one (version 0.34.2, the one with the project's
pulled models). This is **not a Phase-16 defect**: the identical mechanism is already how the
Phase 11+ Docker-Compose stack reaches Ollama (`docker-compose.yml`'s own header), so the same
collision — if present — already affects today's dev/gate stack on a machine with both installed.
Fix is a user/environment choice (stop or disable autostart of the Windows app while developing
SecRAG), not a code change; recorded for the user's decision in the 16-A handback.

**Self-reported boundary 2 — NetworkPolicy is not yet enforced.** k3d's default CNI (flannel,
via k3s) does not enforce `NetworkPolicy` objects — `backend-egress-ollama` is accepted by the
API server today but has no effect on traffic until T16.4.1 (block 16-E) picks a
NetworkPolicy-capable CNI (e.g. Calico). This is by design (T16.4 owns the CNI decision, not
16-A) but is worth stating plainly: nothing in this block's manifests actually blocks traffic
yet.

**S3-compatible store (T16.0.5, resolves TF35).** Verified live: the upstream `minio/minio`
GitHub repository now reads "THIS REPOSITORY IS NO LONGER MAINTAINED" and documents **source-only
distribution** for the community edition starting at release `RELEASE.2025-10-15T17-29-55Z` (0
release assets, versus 48 assets on the prior release `RELEASE.2025-09-07T16-13-09Z`); the
`minio/minio` Docker Hub repository itself returns `401 Unauthorized` even with a freshly issued
anonymous pull token, i.e. it is no longer a usable public image source at all, not just
"unmaintained." **Decision: use SeaweedFS instead of MinIO or Garage.** SeaweedFS is Apache-2.0
licensed (confirmed from its `LICENSE` file) and actively maintained — its latest GitHub release
(`4.55`) was published 2026-11-23, the day before this block ran — with a built-in S3 API gateway
and an official container image still published on Docker Hub
(`chrislusf/seaweedfs`, tag `4.55` present, confirmed via the registry API). Garage (the plan's
other fallback) was considered and rejected for this use: it is AGPLv3 (vs. SeaweedFS's
permissive Apache-2.0) and has a narrower S3 API compatibility history, which matters because
T16.1.5 needs it to work as Barman Cloud's backup target (multipart upload, standard bucket
operations) for CloudNativePG. Pinned image for T16.1.5:
`chrislusf/seaweedfs@sha256:7ca46aeae2f223b37e5d91593c17ab37738eec6df6ab69b48a0487f7643342b9`
(resolved from tag `4.55`). No in-cluster deployment happens in this block — that is T16.1.5's
job; this row only verifies and chooses. *Recommendation for the orchestrator:* fold this choice
into `PHASE_PLANNING.md`'s Phase-16 decision list (e.g. as a new `P16-D5`) alongside P16-D1..D4,
since it is a real architectural decision, not just a toolchain pin.

**Memory budget (T16.0.6).** Idle-cluster RSS measured via `docker stats --no-stream` after the
cluster settled (~20 s post-create): server 488 MiB, agent-0 97 MiB, agent-1 113 MiB, serverlb 24
MiB, registry 6 MiB — **≈ 0.73 GB total**, well inside the plan's ~8 GB envelope for the whole
Phase-16 end state and leaving ~7.3 GB for CNPG, Valkey, Prometheus, KEDA, Kyverno, Mailpit and
Envoy Gateway as they are added in 16-B..16-E, plus native Ollama, the IDE and Docker Desktop's
own overhead. Host-level `free -h` showed `used` rise from 1.7 GiB to 3.3 GiB across the same
window — the extra ~0.9 GiB beyond the five containers' own reported RSS is Docker Desktop's
WSL2-VM-level overhead for running the cluster at all, not attributable to any single container;
it should be re-measured as the full component set lands in later blocks, not assumed constant.
