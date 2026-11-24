# ADR phase 16 — Kubernetes on k3d

- **Status:** **16-A + 16-B (toolchain/cluster + manifests, T16.0.1–T16.1.6) code-complete** on
  `phase-16-kubernetes`, not yet promoted. This ADR records decisions as each block of Phase 16
  lands; it will gain further sections (health/rollouts, autoscaling, cluster security,
  observability) as blocks 16-C…16-F complete.
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
job; this row only verifies and chooses. *Recommendation for the orchestrator:* record this
choice in the Phase-16 decision log (e.g. as a new `P16-D5`) alongside P16-D1..D4, since it is a
real architectural decision, not just a toolchain pin.

**Memory budget (T16.0.6).** Idle-cluster RSS measured via `docker stats --no-stream` after the
cluster settled (~20 s post-create): server 488 MiB, agent-0 97 MiB, agent-1 113 MiB, serverlb 24
MiB, registry 6 MiB — **≈ 0.73 GB total**, well inside the plan's ~8 GB envelope for the whole
Phase-16 end state and leaving ~7.3 GB for CNPG, Valkey, Prometheus, KEDA, Kyverno, Mailpit and
Envoy Gateway as they are added in 16-B..16-E, plus native Ollama, the IDE and Docker Desktop's
own overhead. Host-level `free -h` showed `used` rise from 1.7 GiB to 3.3 GiB across the same
window — the extra ~0.9 GiB beyond the five containers' own reported RSS is Docker Desktop's
WSL2-VM-level overhead for running the cluster at all, not attributable to any single container;
it should be re-measured as the full component set lands in later blocks, not assumed constant.

### 16-B — Manifests: Kustomize base + overlays (T16.1.1–T16.1.6)

**Base layout.** `deploy/k8s/base/{backend,frontend,jobs,gateway,cnpg,services}` plus the
T16.0 `namespace.yaml`/`ollama/`, aggregated by a new `deploy/k8s/base/kustomization.yaml`
(`namespace: secrag` set at the top level — see "self-reported bug" below). No hand-written
HPAs anywhere (KEDA owns scaling from T16.3, TF37). `deploy/k8s/overlays/{local,aks}` sit on
top: `local` is applied for real against the k3d cluster (`ENV=dev` patches on
`backend-config`/`jobs-config`, real `SealedSecret`s); `aks` is rendered/validated only
(`ENV` stays at the base's own `prod` default, `ExternalSecret`/`SecretStore` resources against
Azure Key Vault that name a vault/identity which do not exist on Azure — nothing is ever
`kubectl apply`'d against it).

**Controllers vs. application manifests.** Three Helm-chart controllers — Envoy Gateway
(1.10.0), Sealed Secrets (2.22.0, app `v0.41.2`) and the CloudNativePG operator (0.29.2, app
`1.30.1`) — are installed by `scripts/k8s/cluster-up.sh`, NOT by `deploy/k8s/base`: this is
cluster-level infrastructure (the same category as the k3d registry/Traefik-disable already in
that script), and keeping it out of the kustomize base means `kustomize build` stays
deterministic and network-free (no Helm chart fetch at build time, no `--enable-helm`).
`deploy/k8s/base` holds only the application-facing CRs those controllers reconcile: `Gateway`/
`HTTPRoute`/`GatewayClass` (Envoy Gateway), `SealedSecret`s (the local overlay) /
`ExternalSecret`s (the aks overlay), and the CNPG `Cluster`/`ScheduledBackup`.

**Envoy Gateway (T16.1.1).** A single `HTTPRoute` sends every path to the frontend Service
(its own `app/api/[...path]` BFF catch-all already proxies `/api/*` to the backend
server-side — there is no separate backend route). Two rules, same `backendRef`, different
Gateway API v1 `timeouts` (GEP-1742, supported by Envoy Gateway): `POST /api/chat/stream` (the
SSE answer stream) gets `request`/`backendRequest` = 300s; everything else gets 30s, so a hung
backend fails fast instead of holding a connection open forever. Listener hostname
`secrag.localtest.me` (a public wildcard DNS name resolving to 127.0.0.1, the usual local
Gateway-API-testing convention).

**Database roles Job (T16.1.1, resolves DEC-051).** A new `roles` Job runs `db/roles.sql` +
`scripts/db/apply_roles.sh`/`scram_verifier.pl`, mounted from a `configMapGenerator` that reads
those THREE files directly from the repo (`db/`, `scripts/db/`) — never duplicated into
`deploy/k8s/**`. This requires `kustomize build --load-restrictor LoadRestrictionsNone`
(confirmed: the default restrictor refuses a `configMapGenerator` file source outside the
kustomization's own root) — **every future `kustomize build`/`kubectl apply -k` of this repo's
manifests needs the same flag**, noted here for 16-C's own "manifests" gate step.
**DEC-051's known boundary restated:** this Job packages TODAY's `db/roles.sql`
(`secrag_purger`/`secrag_backup` only); Phase 15's restricted/retention roles land in that same
file only at 15-P, and this Job will start applying them automatically the day that file
changes — no separate code path, read at kustomize-build time.

**CloudNativePG + pgvector (T16.1.4, resolves TF32).** CNPG's official `postgresql` image has
no pgvector; `deploy/k8s/images/cnpg-postgres-pgvector/Dockerfile` builds it from source
(pgvector v0.8.7) in a throwaway builder stage FROM the pinned CNPG base
(`ghcr.io/cloudnative-pg/postgresql@sha256:bf0b0ec...`, tag `16.10-bookworm`), copying only the
compiled extension files into a fresh layer of the SAME base (so CNPG's own
entrypoint/user/metadata are untouched). Built and pushed by hand into the k3d-managed local
registry for this block's own verification; a later block should decide whether
`cluster-up.sh`/CI builds+pushes this image automatically rather than relying on a one-off local
build (not required by any T16.1 Done-when, flagged as a follow-up). **Self-reported boundary:**
a locally built image has no meaningful upstream digest to pin ahead of time (unlike a published
registry image) — reproducibility instead comes from the Dockerfile's own pinned `FROM` digest
and pinned pgvector tag, not from pinning the resulting local image's digest; the in-cluster tag
(`secrag-registry:5050/...`) is mutable across rebuilds until that follow-up exists.

`cnpg/cluster.yaml`: 2 instances (primary + replica), `podAntiAffinityType: required` across
nodes, bootstrap owner = the `secrag-db-credentials` SealedSecret (the SAME credential the
app/Jobs read as `DATABASE_URL` — no secret duplication beyond the one unavoidable copy, the
composed URL also containing the password), `postInitApplicationSQL: ["CREATE EXTENSION IF NOT
EXISTS vector;"]`. Three real CNPG behaviours were discovered only by applying this to the live
cluster, not by reading CNPG's docs in isolation, and are recorded here as self-reported
boundaries future blocks should know about:
1. `shared_preload_libraries` is a CNPG "fixed configuration parameter" — apps may not set it
   directly (rejected: "Can't set fixed configuration parameter"). Not needed anyway: pgvector
   is a pure SQL-level extension with no background worker.
2. CNPG's bootstrap owner role is **not** a superuser (unlike the compose/Azure model, where
   `POSTGRES_USER` always is one) — `db/roles.sql`'s `CREATE ROLE` statements failed with
   "permission denied to create role" under the real owner. `db/roles.sql` is outside this
   block's `touches` and was left unmodified; the fix lives in `cluster.yaml`'s
   `spec.managed.roles` (grants the owner role `createrole: true` — note the lowercase field
   name, confirmed via `kubectl explain`; a first attempt using `createRole` was rejected by
   strict API decoding).
3. CNPG's own Barman Cloud backup support (`spec.backup.barmanObjectStore`) prints a
   deprecation warning on every apply ("will be completely removed in CloudNativePG 1.32.0...
   migrate to the Barman Cloud Plugin"). Accepted as-is for this block (migrating to the plugin
   is a separate operator install, out of scope here) — flagged for whichever later block
   bumps the CNPG chart past the version that removes native support.

`cnpg/scheduledbackup.yaml` is this block's answer to T16.1.6's "backup CronJobs": rather than
re-pointing the Azure-Blob-specific `scripts/db/backup.sh --blob` at a different object store
(which would duplicate what Barman Cloud/CNPG already does correctly for a cluster it manages —
continuous WAL archiving + scheduled base backups + point-in-time recovery), the CNPG
`ScheduledBackup` CR **is** the scheduled-backup mechanism here, confirmed targeting the real
`secrag-pg` cluster daily at 03:00, retention 14d (X9).

**In-cluster services (T16.1.5).** Mailpit and Prometheus reuse the exact same pinned digests
as `docker-compose.yml`'s own services (one image, two dev stacks, no drift). Valkey
(D16.3/P16-D3's future `LimiterStore` backend — the store itself, not yet wired to any limiter
code, which is T16.3/block 16-D) has deliberately no persistence (plain Deployment + `emptyDir`,
matching the plan's own "no persistence" decision) and a password from a new `valkey-credentials`
SealedSecret. SeaweedFS (T16.0.5's choice) runs as a single all-in-one `weed server -s3` process
with its own S3-identity config file (`seaweedfs-s3-config` SealedSecret) sharing the SAME
access/secret key pair as `seaweedfs-credentials` (what CNPG's Barman Cloud client reads) — a
disposable curl request against its S3 port returning `403` (not a connection error) confirms
the gateway is live and actually enforcing that identity, not just listening.
**Self-reported boundary (kube-state-metrics):** the upstream image's container `USER` is
`nobody` (non-numeric) — `runAsNonRoot: true` alone cannot be verified by the kubelet from a
non-numeric image user ("cannot verify user is non-root"); fixed with an explicit
`runAsUser: 65534`, confirmed correct (the pod reached `Running`/`Ready` immediately after).

**Jobs/CronJobs (T16.1.6).** The migration Job (T16.1.1) and the new hourly `purge` CronJob (on
the slim `jobs` image, mirroring `deploy/azure/jobs/purge.yaml`'s own command and `"0 * * * *"`
schedule exactly, running as `secrag_purger`) both still use a PLACEHOLDER `jobs`/`backend`/
`frontend` image tag (X1 convention — building the real application images is a separate CI/CD
concern this block does not own), so their own command logic could not be executed end to end
on this cluster; only their manifest shape, env/secret wiring and RBAC were verified (the SAME
`secretKeyRef` pattern the roles Job already proved correct). **Self-reported boundary
(audit-retention CronJob):** grepped the real repo read-only and confirmed Phase 15's "audit log
= append-only Postgres table" (D15.2) has not merged to `main` yet at this branch's base — no
`audit_log` table, no retention command exists anywhere in `backend/src/rag_app` today. Rather
than inventing a fake command or skipping the row, `audit-retention-cronjob.yaml` ships the
manifest SHAPE now with `suspend: true` and an explicit `secrag.io/known-boundary` annotation,
referencing a placeholder `rag_app.audit retain` module path that does not exist yet — whichever
Phase-15 block adds the real table/command must also flip `suspend` to `false` here.

**Self-reported boundary (namespace bug, found by the cluster smoke, not a design review).** A
`configMapGenerator`-produced ConfigMap (`roles-scripts`) has no `metadata.namespace` of its own
(generators never set one) — on `kubectl apply` it silently landed in the cluster's `default`
namespace, not `secrag`, with **no error at all** (`kubectl apply` happily creates whatever the
current context's default namespace implies). Only surfaced because the in-cluster smoke
(`smoke-16-1-cluster.sh`) actually ran the `roles` Job and watched it fail to mount the volume
("configmap \"roles-scripts\" not found" — in the `secrag` namespace it was looking in). Fixed
with a top-level `namespace: secrag` in `deploy/k8s/base/kustomization.yaml` (Kustomize's
namespace transformer, applied to every resource in the base — correctly left alone the two
cluster-scoped kinds already in the base, `Namespace` and `GatewayClass`, confirmed by
re-building and inspecting their rendered output). **Lesson for 16-C onward:** a resource that
only renders correctly in `kustomize build` output is not proof it lands in the right place —
the cluster smoke is what caught this, which is exactly why this block's prompt asked for one.

**Verification method.** Every claim above that says "confirmed live"/"verified" was checked
against this repo's own real k3d cluster (`scripts/k8s/cluster-up.sh`), never assumed from
reading CNPG/Envoy Gateway/SeaweedFS documentation alone: the local overlay was actually
applied, the roles Job actually ran against a real database, CNPG actually reached "Cluster in
healthy state" with `pgvector 0.8.7` installed, every SealedSecret actually decrypted, and all
five in-cluster services actually reached `Running`/`Ready`. `scripts/k8s/smoke-16-1-cluster.sh`
re-runs this whole chain (19 checks) on demand; `scripts/k8s/smoke-16-1.sh` covers the
cluster-free half (kustomize build + kubeconform + kube-linter on both overlays, the T16.1.2
"aks has no dev-only flag" Done-when).
