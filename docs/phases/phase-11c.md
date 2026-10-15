# Phase 11c — Close-out before Phase 12 (report)

- **Closing:** 2026-10-15 · **Tag:** `phase-11c` (orchestrator creates it on the final deployed
  commit at close).
- **Promoted to Azure:** 2026-10-15 (block E, `main` → `80180e0`, PR #13).
- **Cost:** ≈ €0.004 so far (cap €0.50); 2026-10-15 activity not yet posted at close — see
  Cost below.

## Why this phase existed

Phase 11b closed with a set of decisions, leftovers and process changes still open. Rather than
carry them into Phase 12, the team ran a short close-out phase first: record the 11b decisions,
plan the block split for Phases 12–20, apply the process changes agreed at the 11b close, and
clear a batch of small repo-hygiene debts so Phase 12 starts clean.

## What was done

| Block | What | Result |
|---|---|---|
| A | Decisions + private docs | 11b decisions recorded; 11c added to the private planning docs. No public-facing change. |
| B | GHCR cleanup + cost follow-up | User/orchestrator administrative cleanup of a stray container registry tag and a cost figure confirmation; no code change. |
| C | Block split for Phases 12–20 | Each of the remaining phases split into blocks with task ranges, risk tagging and an estimate; approved by the user. Planning-only, no public doc change. |
| D | Process changes | Internal process changes to how phase work is tracked and reviewed between sessions (a per-phase status archive, a mandatory re-check of every block against earlier changes before it starts, and an explicit decision queue for the user to approve or reject). Process-only, no code or public doc change. |
| E1 | Gate evidence out of `/tmp` | `scripts/gate.sh` now writes its evidence logs (the PASS/FAIL log for each run, used as promotion proof) to a dedicated directory outside the machine's temporary filesystem, created and kept at restrictive permissions (directory `0700`, files `0600`) so a reboot no longer loses the promotion evidence and a shared machine cannot read it. |
| E2 | Flaky test + CI download cache | A backup-pruning test that occasionally failed on a one-second clock boundary was made deterministic by injecting the clock instead of reading the wall clock twice; the CI step that downloads a pinned `shellcheck` release now caches that download (keyed by version and checksum) and retries once on a transient network failure, with the checksum still verified unconditionally. |
| E3 | `private-names` gate step | A handful of public document mentions of private, git-excluded planning file names were reworded to generic terms (for example, "the private planning docs" instead of a specific filename); a new gate/CI step (`private-names`) now fails the build if any tracked, public Markdown file names a private planning file, so this cannot silently reappear. |
| E4 | GitHub Actions on Node 24 | Every GitHub Actions workflow step still pinned to an action release running on the deprecated Node.js 20 runtime was bumped to a release confirmed to run on Node 24 (ten actions across checkout, Node/Python setup, caching, Docker build/push/login, Azure login/CLI, and secret scanning). Confirmed from each action's own release notes: no renamed inputs/outputs, no OIDC/audience behaviour change. |
| E5 | CD change-detection skip-list | The CI/CD pipeline's change-detection skip-list — which decides whether a commit needs a full image rebuild and redeploy — was widened to also skip a verified set of host-only paths (test files, dev/ops scripts, evaluation data, documentation-adjacent assets) that never enter any of the built container images, confirmed by cross-checking the list against every Dockerfile's real copy sources with a dedicated guard test. Paths that do enter an image, or that touch database roles/migrations/CD itself, were deliberately left as deploy triggers. |
| E6 | Latency baseline re-check | The adopted reranker latency baseline was re-measured with the development machine idle (GPU usage 6–8 %) and compared against the committed baseline. Rerank p95 1105.2 ms vs the committed 1104.0 ms, total p95 5912.8 ms vs 5858.3 ms — within normal run-to-run jitter. **Decision: keep the committed baseline unchanged**; the concern that it might be pessimistic (recorded while other load was running on the machine) was not confirmed. |
| Close | This report, tag, debt review | This document; `phase-11c` tag on the final deployed commit; a pass over the open-debt list to confirm what 11c closed and what carries forward. |

## Deviations from the plan (accepted by the user)

- **First `main` push rejected by the `main-ci` branch ruleset.** Renaming a required CI job
  (part of wiring in the new `private-names` step) silently broke the ruleset, which checks
  required status checks by their exact display name. Fixed by restoring the original job
  display name (the new step still runs inside it) and adding a test that pins all five
  required job names verbatim, so a future rename fails locally before it can break a push to
  `main`.
- **Single `gate.sh --full` run at the `main` push.** As in the previous phase's closing fixes,
  a single full gate run was made at the point of pushing to `main` rather than once per
  commit on the phase branch, since the changes in this block were small and mechanical.
- **Demo-guard scheduling runs far less often than configured.** The demo-guard GitHub Actions
  workflow is configured on an hourly `schedule` trigger, but on this low-activity repository
  GitHub actually fired it only roughly every 4–5 hours over the period checked (2026-10-14/15,
  six runs between 10:03 and 09:46 UTC the next day). On 2026-10-15 a demo that ended at
  10:44 UTC stayed on until a manual `off` at 13:29 UTC — nearly three hours past the fixed 3 h
  window. **Decision: accept as-is, no new scheduling mechanism added now; fix the public docs**
  that previously implied a guard lag of roughly an hour. The demo-mode window itself is still
  fixed and self-bounding; the guard is a backstop, and the operator now switches the demo
  `off` as soon as a session ends rather than relying on the guard's timing. A demo switched off
  on time costs roughly €0.50 for a 3 h activation; one left running until the guard catches it
  can cost up to roughly €1.30 (at an estimated €0.15–0.17/h for the three apps together — the
  actual Container Apps billing for this usage is still under review, see Cost below).

## Gate evidence

- `gate.sh --full` **PASS 431 s** on `80180e0` (the block E / close commit).
- CI green on `80180e0` (run `38696935199`).
- CD green on `80180e0` (run `38697466641`), including every newly-bumped action
  (`azure/login` on its OIDC-based release among them) exercised for real.
- Deployed digests: backend `sha256:83027a258ea5355591925212981b029cce7d79927b0656580d6f5f0cc4aa5231`,
  Ollama `sha256:00333ab77cc1f1a7ddec2a540a037127302344de7ef17894e16b737fe5a5f3fd`, frontend
  `sha256:dc0b60a77c6160d3ddd66a86942d5e9e7d0427ee722ff5656996bae78b6efef4`.
- Rollback (previous digests): backend `sha256:8ab9ec16…`, Ollama `sha256:e7dcc48f…`, frontend
  `sha256:cc82902d…`.

## Cost

Phase estimate ≈ €0.05 (cap €0.50). Measured (Azure Cost Management, `ActualCost`, read-only,
whole subscription, queried 2026-10-15 20:45): daily 2026-10-14 **€0.0044**. By resource for
2026-10-06…14: Azure OpenAI €0.0287, Log Analytics / Storage / PostgreSQL Flexible Server
**€0.0000**, and **no Container Apps cost rows at all** — including 2026-10-14, a day with a
CD deploy and a ~7-minute demo activation. Most likely the Container Apps environment's
monthly free grant now covers this subscription's usage in the standard environment, as in
11b (not guaranteed; watched monthly, not assumed permanent). **11c total ≈ €0.004 so far**
against the ≈€0.05 estimate and the €0.50 cap. 2026-10-15 activity (the interview demo, ~5 h
45 min on, plus the PR #14 CI/CD run) is not posted yet at close — tracked as a follow-up, see
Known follow-ups. Subscription total since 2026-09-01: **€6.18 of the €40** student budget.

## Changed docs

[README](../../README.md) (Demo mode — guard-lag wording and per-activation cost), [ADR 11](../adr/adr_phase11_stability.md)
(decision 11 — guard-lag wording and per-activation cost; private planning-file names reworded
to generic terms), [Definition of Done](../DEFINITION_OF_DONE.md) (private planning-file names
reworded to generic terms).

## Known follow-ups

- A stray container registry tag from an earlier phase's bootstrap step is pending
  administrative cleanup (deletion needs the account owner's package-delete permission, not a
  code change); the 2026-10-15 cost figure (interview demo + CI/CD run) was still pending at
  the time of writing (Azure's Cost Management data lags by up to a day) — see Cost above.
- The demo-guard's `schedule` trigger firing far less often than configured is accepted as a
  known gap: the demo window itself is fixed and self-bounding, and the operator switches demo
  mode `off` right after use, but the guard is a looser backstop than its hourly configuration
  suggests. Worth revisiting if a future phase adds a stronger scheduling mechanism (for example,
  an Azure-side scheduled job) on its own merits.
- The CD change-detection skip-list now covers every host-only path verified today, but any
  future path that never enters an image still needs the same verification before being added —
  the guard test only protects the paths already listed.
