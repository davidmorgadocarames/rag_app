# Phase 11b — Stability: rerank latency, baked model images, cold start (report)

- **Closing:** 2026-10-14 · **Tag:** `phase-11b` (orchestrator creates it on `69a35f7` at
  close).
- **Promoted to Azure:** 2026-10-06 (first promotion, `main` → `5a4dc91`, PR #8); block H
  promoted 2026-10-07 (`main` → `e45f250`, PR #10); closing fixes promoted 2026-10-14
  (`main` → `69a35f7`, PR #11).
- **ADR:** [ADR phase 11 — Stability](../adr/adr_phase11_stability.md) (decision 8 — blocks
  A–G — and decision 11 — block H — land in 11b).
- **Why this phase existed:** 11a's reranker reloaded the cross-encoder model on every
  question and ran CPU inference far above a usable floor (the frozen "before" baseline:
  `rerank_inference` p95 9.2 s, `total` p95 11.8 s). On Azure, `secrag-ollama` lost the
  `bge-m3` embedding model on every scale-to-zero (ephemeral storage), forcing a slow re-pull
  on the next cold start. 11b fixes both: one shared, warmed reranker with a faster model,
  model images baked in (reranker in the backend image, `bge-m3` in the Ollama image), and —
  once the first promotion's smoke exposed a new cold-start failure — a slim Ollama image,
  a configurable embed wait, and a cost-bounded demo mode.

## What was done

| Block | Rows | Result |
|---|---|---|
| A | T11.3.1–T11.3.3 | Per-stage answer timing (classify, query embed, hybrid search, rerank load/inference, generate, groundedness, time to first token, total) on both `/chat` and `/chat/stream`, one JSON line per answer, never the question/answer/user id/IP; Prometheus histograms by stage plus an in-flight gauge, served on a separate internal port (the API port never serves `/metrics`); a `prometheus` container in compose |
| B | T11.3.4–T11.3.5 | Explicit `num_ctx` per call type; critical measured finding — generation and groundedness **must** share the same `num_ctx` (an Ollama reload costs ≈6.3 s otherwise); frozen "before" latency baseline (`eval/latency_baseline.json`, fresh reranker per call): `rerank_inference` p50/p95 7913/9204 ms, `ttft` p50/p95 9938/11373 ms, `total` p50/p95 10212/11812 ms |
| C | T11.4.1 (+ DA-11bB-1 fixed first) | Single shared reranker, constructed once at FastAPI startup, reused by `/chat`, `/chat/stream`, the eval gate step and the agentic router; the eval judge and the agentic query rewrite now pin the same `num_ctx` too, since they already share one `OllamaChat` keep-alive window with generate/groundedness |
| D | T11.4.2–T11.4.3 (+ DA-11bC-1, DA-11bD-1/2) | Reranker baked into the backend image (offline by default, `HF_HUB_OFFLINE=1`); the download layer narrowed to the two files it needs and moved before the source copy, so an ordinary code commit reuses the cached layer; custom Ollama image with `bge-m3` baked in; a post-deploy health check (backend `/health`, frontend `/`); `METRICS_PORT`/bind made configurable; CD builds and deploys the Ollama image before backend/frontend (it is called synchronously for embeddings) |
| E | T11.4.4 | Measurement methodology for the "after" numbers: the frozen "before" baseline stays untouched; new `shared`/`warm-single`/`warm-vs-cold`/`--concurrency` modes isolate the shared-instance effect from the model-config effect; two new tests prove correct, independent reranker results under real thread overlap (DA-11bC-2) |
| F | T11.5.1–T11.5.3 | Reranker experiment matrix (model, device, quantization, thread count): adopted **`bge-reranker-base`, int8, 2 threads pinned** — p95 1.02 s (1 thread) / 4.2 s (2 threads) vs the old `bge-reranker-v2-m3` fp32's 5.6 s / 21.4 s; an app-level concurrency cap of 2 keeps p95 at 1.0 s (1×) / 2.4 s (4×) / 6.1 s (10×) against 28.8 s uncapped at 4× |
| G | T11.6b.1–T11.6b.3 + fixes G2, G3 | `latency` gate step becomes a real pass/fail check against the adopted baseline; ADR docs sweep and the 11b rollback block written; **fix G2** closed a gap where `reranker_max_length`/`reranker_quantize` were not part of the adopted-baseline guard; **fix G3** pinned two new frontend HIGH advisories (`sharp`, `source-map-js`) via `package.json` overrides, no `next`/`postcss` major bump; DA pre-promotion review found no blocker — **promoted** (PR #8, `main` → `5a4dc91`) |
| H | T11.6b.6–T11.6b.10 + fix H2 | **Unplanned block, added after the first smoke failed** (see Deviations): configurable embed wait (`EMBED_TIMEOUT_SECONDS`, default 170 s, upper-bounded at 230 s); two-stage slim Ollama image (CPU-only final stage on the same Ubuntu 24.04 base upstream ships, on-disk 4.83 GB → 1.12 GB, bit-identical embeddings); `demo-mode.sh on\|off\|status` (fixed 3 h window, tagged on each app); an hourly `demo-guard.yml` workflow that only ever scales an expired/untagged app down to 0; **fix H2** (DA review): `status` now aggregates all three apps instead of a one-way latch, `on` rolls back everything it touched on failure, and the embed-timeout upper bound is enforced fail-closed — **promoted** (PR #10, `main` → `e45f250`) |
| Fixes | F-H-1, Next.js bump | `demo-mode.sh status`'s "approximate cost so far" now caps at the 3 h window (or prints `n/a`) instead of a nonsense figure for an expired/hand-edited tag; Next.js bumped 15.5.26 → 15.5.27 for GHSA-4jqv-mc3x-m676 and GHSA-mcj8-r9mp-w47p (cache poisoning in SSG/ISR) — **promoted** (PR #11, `main` → `69a35f7`) |

## Deviations from the plan (accepted by the user)

- **First smoke cold start FAILED, unplanned block H.** The first 11b promotion smoke
  (2026-10-06, `main` = `5a4dc91`) passed every warm check but the cold-start question
  answered **HTTP 500 at 183 s**: the backend woke in ~60 s, then Azure pulled the 6.3 GB
  `secrag-ollama` image in ~130 s, and the hard-coded 120 s embed timeout fired before the
  pull even finished. The user approved block H (configurable embed wait, slim Ollama image,
  demo mode + guard) to fix it before the phase could close.
- **Smoke budget re-run with a new 20-question budget.** The first smoke's 19-question
  budget was partly consumed by the cold-start failure without completing every check; block
  H's promotion smoke (T11.6b.10) used a fresh 20-question budget end to end.
- **2026-10-14 ff-merge push rejected by the pre-push gate.** The first push of PR #11's
  ff-merge was rejected by `dependency-audit`: Next.js 15.5.26 in production was affected by
  GHSA-4jqv-mc3x-m676 and GHSA-mcj8-r9mp-w47p (medium, cache poisoning in SSG/ISR, published
  2026-10-07). The user chose to fix it now rather than accept it as a dated risk: Next.js
  bumped to 15.5.27 in the same PR.
- **Single `gate.sh --full` at the main push (user option A).** For that small follow-on fix,
  the user chose to run `--full` only once, at the main push, rather than once per commit.
- **A docs/tests/scripts-only change still triggers a full CD deploy.** CD's change-detection
  skip-list (`plan.sh`) only skips `docs/**`, `**/*.md` and `deploy/k8s/**`; a host-only
  change under `scripts/azure/**` or `backend/tests/**` still rebuilds every image and reruns
  the migration Job. Safe (idempotent) but wastes CI/Azure minutes — left open, see "Known
  follow-ups".
- **Two orphan throwaway smoke accounts left on Azure, no PII.** Two `smoke-11b-<ts>@
  example.com` test accounts from the block H smoke could not be self-erased during the
  session (one lost its token to an idle WSL shutdown, the other's JWT expired first); the
  user chose to leave them and erase both later through the admin erasure panel planned for
  Phase 13.

## Gate evidence

- Final `gate.sh --full` **PASS 456 s** on `69a35f7` (the closing fixes); earlier **PASS
  429 s** on `e45f250` (block H).
- **Local "before" baseline** (block B, frozen, fresh reranker per call): `rerank_inference`
  p95 **9.2 s**. **Adopted baseline** (block F/G, shared + warmed `bge-reranker-base` int8,
  2 threads pinned): `rerank_inference` p95 **1.10 s** — the gate's `latency` step now fails
  below this floor.
- **Block F reranker matrix** (2-thread pinned, p95): `bge-reranker-v2-m3` fp32 5.6 s / fp32
  unconstrained 21.4 s; v2-m3 int8 3.5 s / 14.4 s; `bge-reranker-base` fp32 1.6 s / 6.0 s;
  **base int8 (winner) 1.02 s / 4.2 s**. Concurrency cap of 2: p95 1.0 s (1×), 2.4 s (4×),
  6.1 s (10×) vs 28.8 s uncapped at 4×.
- CI: green on every promoted SHA (`5a4dc91`, `e45f250`, `69a35f7`); one known flaky test
  during the phase (`tests/test_backups.py::…keeps_the_newest_and_refuses_to_guess`, a
  1-second clock-boundary race) and one transient HTTP 500 fetching the pinned shellcheck
  tarball in the gate's cheap-checks step — both passed on re-run.

## Azure promotion evidence

- **Smoke 1 (2026-10-06, `main` = `5a4dc91`, 19 questions).** Embeddings consistent
  (`bge-m3` manifest `790764642607`, cosine 0.99999, 322 chunks). Warm, 12 sequential
  requests: rerank p50/p95 817/981 ms, embed 100/156 ms, generate 1.9/3.7 s, TTFT 2.9/4.8 s,
  total 3.8/6.0 s. 3+3 concurrent: all 200, rerank up to 3.6 s (the cap of 2 queues the
  third), 0 restarts. **Cold start FAILED** — HTTP 500 at 183 s (6.3 GB Ollama image pull
  130 s vs the then 120 s embed timeout) → block H.
- **Smoke 2 (2026-10-07, `main` = `e45f250`, 20 questions, after block H).** Cold (all 3 apps
  at 0) → `/chat` **200 in 110 s**: backend scheduled + image pull 39 s + start ≈55 s; Ollama
  slim-image pull **40.1 s** (was 130 s), container started 43 s after scheduling; in-app
  embed wait 47.3 s, rerank 1.0 s, generate 4.5 s, total 53.6 s. Embeddings cosine
  0.999996/0.99999/0.999989. 12 sequential: 12/12 200, client p50 3.88 s, max 8.33 s. 2×3
  concurrent: 6/6 200, 6.7–9.4 s, 0 restarts. **Demo mode:** `on` → ready in 73 s (72 s on
  2026-10-14), status `on`, 1/1, a question 200 in 8.5 s; `off` → 0/1. **Guard:** a real
  `workflow_dispatch` run (37681780778) → OIDC login OK, scaled down only the one app with an
  expired tag. Slim image **1.12 GB** on disk vs 4.83 GB for the fat image (both from
  `docker image inspect`).
- **Deployed digests.** First promotion (`5a4dc91`): Ollama (fat) `rag_app-ollama@sha256:
  b42072fd32731e0456cce1128f878b6f98aaf048538e3be55a757f7da131cf03`, backend
  `rag_app-backend@sha256:8e7697be0347a7a18b5730dda57bd9019ac3fd5d5115e890ba771796d41d80e2`,
  frontend `rag_app-frontend@sha256:2b1bb0f24905c2335a040e110df43558978804c1b55ebcd71166a3e1498a2e8f`.
  Block H promotion (`e45f250`): Ollama (slim) `@sha256:6fdd5acf7b7b9d679e0ef4191cef1b4e41a6c978475c01a72f0378b2acef0302`,
  backend `@sha256:bd76f69333d4fd67b57e8137d927bc0359a0324280f83acb23e4a424198242f6`, frontend
  `@sha256:3ddc431fa8bfb0286849ad943207fb335bbddbe52eccd74fb3ade41f266265b8`. Closing fixes
  (`69a35f7`): backend `@sha256:8ab9ec16…`, Ollama `@sha256:e7dcc48f…`, frontend
  `@sha256:cc82902d…`. All apps min 0 / max 1, no stray tags, ingress on port 8000 only after
  every promotion.
- **User manual test (2026-10-14, demo mode).** All features worked; rerank perceived at
  ≤ ~2 s, described by the user as "huge latency improvement" over 11a.

## Cost

Phase estimate ≈ €0.15–0.35 (cap €1). Measured (Azure Cost Management, read-only, whole
subscription): 2026-10-06 **€0.0106** (first promotion + smoke), 2026-10-07 **€0.0137**
(block H promotion + smoke), 2026-10-08…13 **€0.00**, 2026-10-14 not yet posted at close (the
service lags by up to a day; re-checked the next day, following the same pattern as 11a's
row 43). By resource for 2026-10-06…14: Azure OpenAI €0.0243, everything else (Container
Apps, Postgres Flexible Server, Storage, Log Analytics) **€0.00** — Container Apps itself
shows €0 in the standard `secrag-cae` environment, most likely the monthly free grant now
covering this subscription's usage (open question, not assumed permanent). **11b total
≈ €0.025** against the ≈€0.15–0.35 estimate and the €1 cap. Subscription total since
2026-09-01: **€6.17 of the €40** student budget. See
[ADR 11 — Costs](../adr/adr_phase11_stability.md#costs) for the 11a row-43 figure (€0.0534 on
2026-10-01) folded in at this close.

## Changed docs

[ADR 11](../adr/adr_phase11_stability.md) (Azure numbers, costs, rollback slot), [phase 11a
report](phase-11a.md) (row-43 cost figure), [README](../../README.md) (demo mode),
[DEBT_REGISTER-tracked follow-ups folded into "Known follow-ups" below].

## Known follow-ups

- Running Ollama 24/7 (no scale-to-zero) was considered and **not approved**: at Azure
  Container Apps' per-vCPU-second/GiB-second price this is ≈€1.8/day (≈€54/month) for the
  Ollama app alone, against a far smaller annual budget. Demo mode + the hourly guard is the
  accepted interim approach.
- Rerank concurrency is capped at 2 in the app, with no autoscaling — real load above that
  queues (p95 6.1 s at 10×). Accepted until KEDA-based autoscaling (Phase 16.3) replaces
  today's fixed replica count.
- The demo-guard GitHub Actions workflow runs on a `schedule` trigger, which GitHub disables
  automatically after 60 days with no commits/pushes to the repository; its only failure
  signal today is GitHub's default "email the actor" behaviour. Accepted as-is because the
  demo's 3 h window is self-bounding (the guard is a backstop, not the cost ceiling), but
  worth checking after any quiet period of 45+ days.
- CD's change-detection skip-list only skips documentation/markdown/Kubernetes-manifest
  changes; a host-only ops-script or test-only change still triggers a full image rebuild and
  redeploy. Safe but wasteful — a decision on widening the list is due early in the next
  phase that touches CD.
- Backend sizing at "~1 vCPU / 2 GiB" is confirmed **not viable** with the adopted reranker
  configuration — measured RSS stayed close to 2 GiB warm even after quantization. A real
  Azure right-sizing measurement is needed before any smaller SKU is attempted.
- Two orphan throwaway smoke accounts on Azure (no PII, one question each) are pending
  erasure through the admin panel once it ships (Phase 13).
- A flaky backup-pruning test (`tests/test_backups.py`, a 1-second clock-boundary race) and a
  transient shellcheck-tarball download failure in the gate's cheap-checks step both surfaced
  during this phase; both passed on re-run and are candidates for hardening early in the next
  phase.
