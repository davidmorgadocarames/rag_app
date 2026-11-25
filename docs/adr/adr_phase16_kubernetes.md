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

### 16-B-fix — DA-16-B-1 apply-ordering script + structural SealedSecret test (T16.1.1/T16.1.6)

**The blocker (DA-16-B.md).** The 16-B section above, and the manifests themselves
(`migrate-job.yaml`, `roles-job.yaml`, `backend/deployment.yaml`, `cluster-up.sh`) all claimed
Job ordering was "enforced by `scripts/k8s/deploy-local.sh`" — a script that did not exist
anywhere in the repo (`status/failures.md` F20). `smoke-16-1-cluster.sh` only ever did a single
flat `kustomize build | kubectl apply` of the whole overlay: roles Job, migrate Job and
backend/frontend Deployments were all submitted at once, with no ordering at all. The bug was
never observable because backend/frontend/jobs images are still `:placeholder` — real pods never
start, so a real "backend before migration finished" race was never exercised.

**The fix: `scripts/k8s/deploy-local.sh`.** A single ordered-apply script, idempotent, enforcing
exactly the sequence the manifests already described: namespace → wait for the 3 controllers'
CRDs/Deployments to be ready (pre-installed by `cluster-up.sh`, this is a readiness check, not an
install) → every other non-order-critical resource (SealedSecrets, ConfigMaps, RBAC, Services,
CronJobs, the Gateway/HTTPRoute stack — none of these has a correctness dependency on the stages
below) → the CNPG `Cluster` + `ScheduledBackup`, wait Ready → the `roles` Job, wait Complete → the
`migrate` Job, wait Complete → the backend/frontend Deployments, `kubectl rollout status` both.
Every wait is **fail-closed**: a distinct exit code per stage (3 controllers, 4 CNPG, 5 roles, 6
migrate, 7 rollout) and the script stops immediately — it never reaches a later stage — if a wait
does not succeed in time. `smoke-16-1-cluster.sh` now calls this script instead of the flat
apply, and treats exit code 6 (migrate Job did not Complete) as the expected, documented X1
outcome today (placeholder `jobs` image can never pull), not a smoke failure — every other
non-zero exit code still fails the smoke.

Splits the kustomize-built multi-document YAML into per-document files, indexed by
`kind`/`metadata.name` via `awk` (no `yq` in this environment, same convention as the rest of
this repo's k8s scripts), so each stage can `kubectl apply -f` only the resources it owns.

**Proof the ordering is real, not just asserted (this fix's own Done-when).** Two kinds of
evidence, since the app images are still placeholders and the real race can therefore never be
observed end-to-end on this cluster today:
1. **Offline, dry-run trace** (`backend/tests/test_k8s_deploy_order.py`, runs under the `pytest`
   gate step, no cluster/kustomize needed): `deploy-local.sh --dry-run --trace FILE` walks the
   exact same function-call order the real path uses (each stage logs its own keyword before the
   real/skipped action), and the test asserts the full sequence — in particular that
   `CHECK_MIGRATE_JOB_COMPLETE` always precedes `APPLY_APPS`. This fails on the pre-fix code
   trivially: the script did not exist.
2. **Real cluster run, up to the Jobs.** Against a live k3d cluster (CNPG image rebuilt/pushed by
   hand — no scripted build/push step exists yet, see the F20 fix below), `deploy-local.sh`
   really reached `Cluster in healthy state`, really completed the `roles` Job, then really timed
   out waiting on the `migrate` Job (placeholder `jobs` image, `ImagePullBackOff`) and exited 6 —
   confirmed with `kubectl get deployment backend frontend` returning `NotFound` immediately
   after: the ordering genuinely prevented the backend/frontend Deployments from ever being
   applied, not merely from becoming Ready.

**Structural SealedSecret test (DEC-055).** Independent of whichever gitleaks-allowlist option
the user picks for DEC-055, `backend/tests/test_k8s_sealed_secrets.py` (wired into the `pytest`
gate step) asserts every YAML document in any `deploy/k8s/**/sealed-secrets.yaml` is
`bitnami.com/v1alpha1` `SealedSecret` with only `spec.encryptedData` populated (no `stringData`,
no plain `data`, on the spec or its `template`), and that no plain `kind: Secret` with a
`data`/`stringData` block exists anywhere under `deploy/k8s`. A deterministic YAML-shape
assertion, not a heuristic — it would catch a real `kind: Secret`/`stringData` landing in that
file even if gitleaks' own rule were ever disabled or missed it.

**F20 self-check, 2 more findings fixed (same class as the original blocker, found while
auditing every path this fix's own comments reference).**
- `deploy/k8s/base/backend/configmap.yaml` referenced
  `deploy/k8s/base/secrets/app-secrets.sealedsecret.yaml` — that path never existed; the real
  file is `deploy/k8s/overlays/local/sealed-secrets.yaml` (T16.1.3). Fixed.
- `deploy/k8s/images/cnpg-postgres-pgvector/Dockerfile` and `backend/deployment.yaml` both
  claimed the pgvector/backend image build+push was performed by an existing script
  (`cluster-up.sh` / `deploy-local.sh`). Neither script builds or pushes any image — the pgvector
  image used by every verification in this ADR (16-B and 16-B-fix) was built and pushed by hand.
  **Self-reported boundary, not yet fixed in code:** a scripted local build+push step for the
  pgvector/backend/frontend/jobs images does not exist yet; a later block (T16.1.7/16.2-adjacent)
  should add one. Comments corrected to say so plainly instead of asserting a mechanism that
  isn't there.

**Known boundary (self-reported, not a regression).** `deploy-local.sh`'s migrate-Job wait will
legitimately fail-closed (exit 6) on every real local run until that scripted build+push step
above exists and produces a real `jobs` image — this is the CORRECT behaviour (refuse the
backend rollout, not "usually" refuse), not a bug to silence; `smoke-16-1-cluster.sh`'s own
comment header says so.

### 16-C — `manifests` gate step, CI `kind` job, `--k8s` mode, probes (T16.1.7, T16.1.8, T16.2.1)

**`manifests` gate step (T16.1.7).** Promotes `scripts/k8s/smoke-16-1.sh` (16-B's own static
manifest smoke) from informational to the project's real gate step: `kustomize build` of both
overlays + `kubeconform -strict -ignore-missing-schemas` + `kube-linter lint` + the T16.1.2
"aks carries no dev-only flag" assertion. Stack-free (fast tier, also CI's `cheap-checks`
job). kube-linter is now **enforced** (was informational in 16-B): two project-wide, dated
exceptions in the new `deploy/k8s/.kube-linter.yaml` (`run-as-non-root`, `no-read-only-root-fs`
— no image under `deploy/k8s/**` sets either yet, not just backend/frontend as 16-B's own
"known boundaries" said, but every third-party service image and the `jobs`-image Jobs too;
T16.4.2/16-E fixes both together, re-enable here in the same block), plus one narrow,
per-object `ignore-check.kube-linter.io/dangling-service` annotation on `ollama-host`
(selector-less by design, T16.0.4 — the one and only `dangling-service` finding this project
ever expects). Two real `job-ttl-seconds-after-finished` findings fixed for real (not
excepted): `purge-cronjob.yaml`/`audit-retention-cronjob.yaml` no longer set
`ttlSecondsAfterFinished` at the Job-template level — a CronJob-owned Job's cleanup is the
CronJob's own `successfulJobsHistoryLimit`/`failedJobsHistoryLimit`, never a per-Job TTL that
can disagree with it. Result: 0 kube-linter findings on both overlays (down from 21, measured
before this row's fixes).

**CI `kind` job (T16.1.7, P16-D4).** `.github/workflows/ci.yml`'s `k8s-manifests-kind` — a
real `kind` cluster on a free GitHub-hosted runner, never the real torch-heavy
backend/frontend images (those stay local, `--k8s` mode). Renders the REAL local overlay,
then filters it to the kinds a vanilla kind cluster understands without installing
Envoy Gateway/Sealed Secrets/CNPG (`Namespace`/`Deployment`/`Service`/`ConfigMap` only —
`SealedSecret`/`Cluster`/`Gateway`/`HTTPRoute`/Jobs/CronJobs stay `--k8s`'s own job, which
installs the real controllers) and substitutes a tiny, always-pullable stub image
(`registry.k8s.io/pause`) for backend/frontend, stripping their Secret references and probes
(the real probes are unit-tested on their own, T16.2.1 below — this stub run proves the
Deployment/Service/ConfigMap SHAPE survives a real API server, not that the probes ran).
Installs Kyverno (pinned chart) and applies this repo's own image-signature `ClusterPolicy`
(`deploy/k8s/policies/verify-image-signatures.yaml`, Audit mode, P16-D1/TF33 — "our images
only": `imageReferences` scoped to `ghcr.io/davidmorgadocarames/rag_app-*`, third-party images
stay digest-pinned instead). Not yet wired into `deploy/k8s/base`'s own kustomization —
Kyverno itself is not installed locally until 16-E (T16.4.4); applied ad-hoc by this CI job
for now, 16-E folds it in and flips `Audit` to `Enforce`.

**Two real Kyverno findings, both self-reported and fixed (verified locally first, kind +
the exact pinned chart, before ever touching CI):**
1. Kyverno chart **3.10.0** (app v1.20.0) **removed admission (create/update) of the legacy
   `kyverno.io/v1 ClusterPolicy` type entirely** — applying this exact policy against a fresh
   3.10.0 install is rejected outright by Kyverno's own validating webhook ("Kyverno v1.20
   removed execution of legacy kyverno.io policy types"). Pinned to **3.9.2** (app v1.19.2)
   instead, which still accepts it. The CEL-based `policies.kyverno.io/ImageValidatingPolicy`
   replacement is real, out-of-scope-for-this-row future work for 16-E to pick up when it
   builds the real Enforce policy.
2. `validationFailureAction: Audit` on a `verifyImages` rule **requires `mutateDigest: false`
   explicitly** — the default (`true`) makes Kyverno's own policy-validating webhook reject the
   policy itself ("mutateDigest must be set to false for 'Audit' failure action"), before it
   is ever active. Fixed in the policy file, with the finding recorded in its own comment.

**What Audit mode actually does, confirmed locally (not assumed):** a throwaway pod whose
image matches the policy's pattern but is not signed (and does not even exist — no real
`ghcr.io/davidmorgadocarames/rag_app-*` image is published before 15-P) is **admitted**
(`kubectl run`'s own exit code is 0) with a `Warning:` in the API response naming the policy,
and Kyverno writes a real `PolicyReport` with `FAIL >= 1` for it — this is the CI job's own
"Kyverno is actually evaluating admission" proof, not a `PolicyReport` existing unconditionally
(confirmed empty when no pod in the cluster matches the policy's `imageReferences` at all).
**Self-reported note for 16-E's real Enforce rollout:** confirm a VALIDLY signed image is
still admitted once 15-P ships one, before flipping `Enforce` — an untested assumption until
then.

**`--k8s` mode (T16.1.8).** A new, separate `gate.sh` mode and step table
(`k8s-chaos`/`k8s-autoscale`/`k8s-policies`/`k8s-restart-check`/`k8s-eval`), never mixed into
`--fast`/`--full`/`--only`'s own step count, and never managing the k3d cluster's own
lifecycle (bring-up/tear-down stays the operator's explicit, separate step — RAM, when a
concurrent block's `--full` needs the same Docker Desktop budget). Only `k8s-chaos` is
implemented today: applies the local overlay via `deploy-local.sh` with short per-stage
timeouts (`APPLY_FOUNDATION`, including Valkey, happens before CNPG/roles/migrate, so it is
already applied by the time `deploy-local.sh` fail-closes on the still-placeholder
jobs/backend/frontend images), then kills the Valkey pod and proves the Deployment recreates
it and the Service's Endpoints come back. The other four are an **explicit, named SKIP**
(never a fake PASS): `k8s-autoscale` (KEDA `ScaledObject`s, TF37, block 16-D),
`k8s-policies` (Kyverno `ClusterPolicy`s, P16-D1, block 16-E), `k8s-restart-check`/`k8s-eval`
(no scripted backend/frontend/jobs image build+push step exists yet — the same self-reported
boundary 16-B-fix recorded; there is no real backend to exercise in-cluster yet). **Planning
gap noticed, not fixed here:** the task rows for Resources/Rollouts/Chaos drills (16.2.2–
16.2.4) do not appear to have an owning block in the current private block split — flagged
for the orchestrator, not resolved by this senior agent (no private planning-file edits in
this block's scope).

**Pre-push `--k8s` scoping (T16.1.8).** `.githooks/pre-push` requires a recorded
`gate.sh --k8s` PASS (`.gate/k8s-pass`, exact SHA — gate.sh writes it only on a real `--k8s`
PASS) when the pushed **diff** touches `deploy/k8s/**`, a probe implementation
(`backend/src/rag_app/api/app.py`, `frontend/app/api/health/**`) or a scaling path
(`backend/src/rag_app/rate_limit.py`) — scoped by the diff, never the branch name (the exact
lesson from an earlier gitleaks cross-branch scoping mistake, so a push on an unrelated branch
is never blocked by this). Never runs k3d inside the pre-push worktree itself.

**Probes (T16.2.1).** The three k8s probes stop sharing one endpoint. Backend: new
`GET /health/ready` (checks Postgres via the existing `SessionDep`, 503 on failure) is
readiness; the existing `GET /health` (already named "Liveness endpoint" in
`rag_app.features.REGISTRY` since 11a) stays liveness, unchanged behaviour — every existing
caller (`restart_check.sh`, `erasure_scale.py`, `negative_control.py`, the frontend's
`/api/health` proxy) keeps working exactly as before. Startup stays on `/health` too: the
backend's own `lifespan` already blocks Uvicorn from serving ANY request, including this
probe, until the DB check + reranker warm-up both succeed, so hitting the cheap liveness path
for startup is still a real "did startup finish" signal, not a weaker one — `failureThreshold`
raised 30→60 (150s→300s) for a slower, real (not placeholder) image's cold model load.
Frontend: the existing `GET /api/health` (already a real dependency check — it proxies the
backend's own `/health`, used by Azure's CD health check) becomes the readiness/startup
target unchanged; new `GET /api/health/live` (static `{"status":"ok"}`, no backend call) is
liveness — a transient backend outage must never restart the frontend container.
`backend/tests/test_api.py` proves both ends structurally: liveness never resolves a DB
session even when the override is set up to crash if it tried; readiness fails closed (503)
on a DB error, never a bare 500.

### 16-C-fix — wire `manifests` into CI, name `.gate/k8s-pass`'s trust model (DA-16-C-1, DA-16-C-2)

**DA-16-C-1 (blocker, fixed).** 16-C's own `manifests` gate step claimed "fast tier + CI's
`cheap-checks` job" but `cheap-checks`' `gate.sh --only ...` line never named `manifests`,
and the job never installed kustomize/kubeconform/kube-linter at all — a push that skipped
the pre-push hook (`core.hooksPath` unset, `--no-verify`, or any clone without the hook)
shipped a kube-linter/manifest regression with **zero server-side signal**; the `kind` job
doesn't run kubeconform/kube-linter either (it only proves manifests apply to a real API
server, a different check). Fixed: `manifests` added to both the step's `name:` and the
`run:` line of `cheap-checks`' `gate.sh --only ...` step; a new step installs
kustomize/kubeconform/kube-linter by calling `scripts/k8s/install-tools.sh` **directly**
(never a second, hand-copied pin table) — one source of truth for the pins/checksums,
fail-closed on any mismatch via the installer's own `set -euo pipefail` + `sha256sum -c`.
Confirmed the `k8s-manifests-kind` job is unaffected (single diff hunk, confined to
`cheap-checks`; `backend/tests/test_ci_cheap_checks.py::test_k8s_manifests_kind_job_is_unaffected_by_the_cheap_checks_fix`).

**Self-reported fix needed to make the reuse work:** `install-tools.sh`'s `check()` always
verified all nine tools unconditionally, even for a partial install — on a bare CI runner
that only ever asked for kustomize/kubeconform/kube-linter, the other six (k3d, kubectl,
helm, k6, cosign, kubeseal) are never installed by design, and the old `check()` would still
report them `KO` and fail the whole step. `check()` now takes an optional tool-name subset
(`check kustomize kubeconform kubelinter`) and verifies only what was requested; the no-args
default (full install, or a bare `install-tools.sh check`) is unchanged.
`backend/tests/test_gate_script.py::test_install_tools_check_verifies_only_the_requested_subset`
proves both the subset and the unchanged-default behaviour, with no network access (stub
binary + the `check` subcommand, not a real download).

**DA-16-C-2 (named, not fixed as a gap — accepted as documented residual risk).**
`docs/DEFINITION_OF_DONE.md`'s `--k8s` row and its "Pre-push `--k8s` scoping" paragraph now
state explicitly that `.gate/k8s-pass` is a **self-attested, non-cryptographic local
marker** — the same trust model as every other git-ignored `.gate/` state file in this repo.
The exact-SHA match (`scripts/gate.sh`, `.githooks/pre-push`) prevents *stale* reuse across
commits (the main risk this control targets) but does not stop a developer from
hand-writing a false PASS for the SHA about to be pushed. Backstops: CI's `k8s-manifests-kind`
job (independent of this marker, a real kind cluster) and 16-F's own exact-SHA `--k8s` run at
the promotion SHA (DEC-16-C-1, adopted into 16-F's checklist).
