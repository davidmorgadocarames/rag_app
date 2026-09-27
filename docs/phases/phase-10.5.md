# Phase 10.5 — Pre-11 hotfixes (report)

- **Closed:** 2026-09-27 · **Tag:** `phase-10.5` (annotated, on `dee9cbc`)
- **Why this phase existed:** a review of the live Phase 10 deployment found issues that had to
  be fixed on Azure before starting Phase 11 (stability).
- **ADR:** no new ADR; the Azure design stays in
  [ADR phase 10 — Cloud deployment (Azure)](../adr/adr_phase10_azure.md).

## What was done

| Item | Result | Evidence |
|---|---|---|
| 10.5.1 Anonymous `POST /chat` | Closed: the endpoint now requires login (kept because the README documents it); tests for 401 / 200 / 422 | `53ee198`, merge `74b2314`; on Azure an anonymous `POST /chat` returns 401 |
| 10.5.2 Scale rule | Every Container App create/recreate uses `--min-replicas 0 --max-replicas 1`, followed by a scale check (private runbook) | Runbook |
| 10.5.3 Resource providers | Compute, ContainerService, Communication, Monitor and KeyVault registered; quotas recorded | Runbook |
| 10.5.4 Budget | Budget of €40 (annual grain) with actual-cost alerts at €10 / €20 / €30 / €40 and a forecast alert | Azure Cost Management |
| 10.5.5 Frontend patch | Next.js 15.1.3 → 15.5.26, React 19.0.0 → 19.0.8, `eslint-config-next` 15.5.26 | `7de632a`, merge `367a7dd` |
| 10.5.6 Azure OpenAI data zone | **Blocked:** DataZoneStandard quota is 0 in every EU region → GlobalStandard kept as a documented, accepted risk | Quota query |
| T10.5.7 CD preparatory commit | `workflow_dispatch` with `dry_run` (default `true`), an echo-only `workflow_run` job after CI on `main`, main-only guards on `images`/`deploy`, deployments recorded through the REST API without an `environment:` key (the OIDC subject stays `ref:refs/heads/main`) | `dee9cbc` |
| T10.5.8 Azure OpenAI capacity cap | No change needed: the gpt-4.1-mini deployment has been at capacity 10 (10K TPM / 10 RPM) since it was created | Read-only `deployment show`; one chat answer on the live app |

Residual from 10.5.5: a `postcss` advisory inside Next.js (build time only) is carried as an
audit exception (file added in Phase 11a) until Phase 15.

## Deviation from the plan (accepted 2026-09-27)

**T10.5.7.** The plan asked for a fixed `concurrency: cd-main` group and for `images`/`deploy` to
echo instead of acting on dry runs. What shipped:

1. The concurrency group is **computed**: real deploys use `cd-main`; dry-run and echo runs use a
   per-run group.
2. Dry runs are a **separate echo job**; `images` and `deploy` are skipped instead of echoing.

**Why:** with a fixed workflow-level `cd-main` group and `cancel-in-progress: false`, GitHub keeps
only one pending run per group, so a queued echo or dry run could replace (cancel) a pending real
deploy. A separate job also keeps the real jobs free of dry-run branches.

## Gate evidence

- Local gate (`scripts/gate.sh`, pre-push): PASS for `dee9cbc`; the eval step was **skipped**
  because the local stack was down (Docker Desktop's WSL integration off). Acceptable for a
  workflow-only change; later phases that touch the app need the stack up. `actionlint` clean.
- CI on `main`: run 36318153252 ✓.
- CD on push: run 36318153270 ✓ — images built and deployed, dry-run and echo jobs skipped;
  deployment record 6692052319 → `success`.
- Echo job after CI (`workflow_run`): run 36318327621 ✓.
- Dispatch `dry_run=true` on `main`: run 36318780830 ✓ — only the dry-run job ran, no new
  deployment.
- Dispatch `dry_run=false` from a temporary branch: run 36318863934 ✓ — "Not main", only the
  dry-run job ran, still one deployment; the branch was deleted afterwards.
- Scale check afterwards: every Container App at min 0 / max 1.

## Cost

Budget for the phase ≈ €0.10. The hotfix deploys and the T10.5.7 push were image rollouts of an
otherwise idle, scale-to-zero app plus a few smoke requests: a few cents, within budget. No new
Azure resources were created.

## Issues found during the smoke (moved to Phase 11a)

- **Owner account unreadable:** the owner's `wrapped_key` does not unwrap with the running master
  key (`InvalidToken`); pre-existing, first seen before this phase's deploys. Diagnosed in
  **11.1** before any Azure change.
- **Orphan conversations on stream errors:** `/chat/stream` creates the conversation and stores
  the user message before generating, but returns the conversation id only in the final event;
  when generation fails the client never learns it, so every retry starts a new conversation.
  Fix in 11a (id in the first event, error event + UI message).
- **Verification link logged in clear** when SMTP is not configured → log hygiene in 11a (R6-5).
- **Ollama loses `bge-m3` on scale-to-zero** (ephemeral storage) → wake-up procedure in the
  runbook for now; permanent fix with baked model images in **T11.4.3** (11b).
