# ADR phase 13 — Admin dashboard + CD per-image build

- **Status:** **13-0 (CD per-image build) and 13-A (feature registry + missing metrics)
  code-complete** on `phase-13-dashboard`, not yet promoted. This ADR records decisions as
  each block of Phase 13 lands; it will gain further sections (admin API, dashboard UI,
  defence lab, live updates) as blocks 13-B…13-P complete.
- **Related:** the private Phase 13 planning record (P13-D1…D6),
  [ADR phase 11](adr_phase11_stability.md) (the CD pipeline this phase extends — plan,
  gate-status, images, migrate, deploy jobs; the digest-pinning and no-rollback invariants),
  [`.github/workflows/cd.yml`](../../.github/workflows/cd.yml) (header comment).

## Context

Since Phase 11a, `cd.yml` built and pushed all 4 images (backend, frontend, jobs, ollama) and
updated all 3 Container Apps on every real deploy, even when a commit only touched one of
them. Two costs followed directly from that: `secrag-ollama` got a new image digest on
**every** deploy (its build layer always produces a new timestamp even with no source
change), forcing an Ollama revision and a ~GB cold start unrelated to the actual change; and
every rollback block had to list all 3 app digests and the jobs image, even when only one
component had actually moved.

## Decisions

### P13-D5 — CD per-image build (path-based; unchanged apps/Jobs keep their deployed digest)

`scripts/cd/plan.sh` already diffs the changed files vs the last deployed SHA to decide
build/deploy at all (docs-only skip, T11c.E5). T13.CD.1 extends it to also map those files to
the 4 images that actually need a new build, and writes 4 new outputs — `build_backend`,
`build_frontend`, `build_jobs`, `build_ollama` — alongside the existing `deploy`:

- `backend/**` → backend **and** jobs (both built from the `./backend` context — see
  `backend/Dockerfile` and `backend/Dockerfile.jobs`);
- `frontend/**` → frontend;
- `ollama/**` → ollama;
- `.github/workflows/cd.yml` or `scripts/cd/**` (this pipeline's own logic, self-referential
  risk) → all four;
- any other non-skippable, unmapped path (e.g. `scripts/db/**`, `db/roles.sql`,
  `deploy/azure/jobs/*.yaml`, any other workflow file) → all four — **fail-safe: build more,
  never less**, rather than trying to maintain an exhaustive allow-list of every path that is
  provably safe to narrow;
- a new `--force-all` flag (and the matching `force_all` workflow_dispatch input, usable on
  **both** dry runs and real runs, unlike `base_sha`/`head_sha`/`skip_tip_check`/
  `simulate_failure` which stay dry-run only) forces all four once a deploy is already going
  ahead. It deliberately does **not** turn a safety skip (not the branch tip, an ancestor of
  the deployed SHA, docs-only, nothing changed) into a deploy — the no-rollback invariant
  (DA-C-1, ADR phase 11) stays absolute regardless of `--force-all`.

T13.CD.2 wires these flags into `cd.yml`: the `images` job's 4 build-push steps each run
`if: needs.plan.outputs.build_<image> == 'true'`. A new script, `scripts/cd/
resolve_image.sh`, resolves the image reference each app/Job actually deploys with: either
the digest just built in this run (`--build true --built-digest …`), or — when that image was
not flagged — the digest **currently deployed** on the app (`az containerapp show`) or Job
(`az containerapp job show`), read fresh every real run. Either way the result is verified to
be pinned by digest (`@sha256:…`); an unreadable `az` call or a kept image that somehow is not
digest-pinned (e.g. a manual `:latest` edit outside CD) **fails the pipeline closed** rather
than silently deploying/keeping a floating tag.

- **The migration Job always runs on every real deploy**, regardless of `build_jobs` — a
  deliberate simplification over a conditional migration step. Justification: (a) `backend/
  **` changing always flags `build_jobs` too (both are built from the same `./backend`
  context), so "migrations must run when backend changed" already holds structurally; (b) for
  the remaining case (jobs image unchanged, e.g. a frontend-only deploy), `alembic upgrade
  head` is idempotent and fast, so running it anyway costs nothing but buys one invariant that
  never needs re-reasoning about: the migration Job executes, and is recorded in its own
  history, on every single real deploy, full stop. It resolves its own jobs image via
  `resolve_image.sh job` (built, or read from the migration Job's own currently deployed
  image) before starting.
- **An app the plan did not flag is never passed to `az containerapp update`** — it keeps
  running the image resolved above (itself a sanity check: a kept app's current image must
  still read back as digest-pinned, or the run fails). The ollama-before-backend/frontend
  order (T11.4.3, ADR phase 11) is unchanged when ollama is among the apps that do update.
- **The purge/backup Job image update only runs when `build_jobs` is true** — when the jobs
  image did not change there is nothing new to *push*, since `migrate` already resolved and
  verified that same (unchanged) digest moments earlier in the same pipeline run. That digest
  is not re-pushed, but it IS cross-checked (DA-13-0-1): `deploy` reads the purge/backup Jobs'
  own currently deployed image (`resolve_image.sh job --build false`) and fails closed, naming
  the Job and both digests, if either differs from the migration Job's resolved image — per-
  image CD means `build_jobs` can stay false across many deploys (e.g. a long frontend-only
  streak), so an out-of-band fix applied by hand to only one of the three Jobs during an
  incident must not go unnoticed. No auto-repair: a mismatch stops the run for a human to look
  at it. DA-13-0F-1: this cross-check runs early in `deploy`, right after each app's image is
  resolved and verified digest-pinned, and BEFORE the deployment is recorded or any app is
  updated — a drift must stop the run before anything changes, not after (it originally ran
  after the app updates and the post-deploy health checks, which only caught drift once the
  apps had already moved).
- The dry-run echo job prints a per-image **BUILD/KEEP** line for all 4 images and a per-app
  **UPDATE/KEEP** line for all 3 apps, so a `workflow_dispatch` dry run against a real commit
  pair shows exactly what a live run would (and would not) touch, with no Azure call needed
  (dry runs never read from Azure — the placeholder/echoed text only *describes* what a real
  run's `resolve_image.sh` would do).

**Rejected alternatives** (recorded at the private Phase 13 planning re-check): (B) make only
the Ollama image conditional, leaving the other three unconditional —
cheaper to build but keeps the exact same rollback-bookkeeping cost for backend/frontend/jobs;
(C) reproducible builds (`SOURCE_DATE_EPOCH` + timestamp rewriting) so an unchanged image keeps
its digest without a path-based plan at all — rejected as unproven (whether Azure Container
Apps treats a re-pushed, same-digest image as "no new revision" was never verified) and it
would still rebuild every image's layers on every run; (D) defer to Phase 15/16 — rejected,
since 13-P (the admin dashboard promotion) is the first real beneficiary and every Phase 13
promotion in between would otherwise keep paying the Ollama cold-start cost.

**Consequence:** a frontend-only change no longer creates an Ollama (or backend) revision; a
backend-only change rebuilds backend+jobs and runs migrations, but leaves ollama/frontend (and
the purge/backup Jobs) untouched; a rollback block only needs to list the digests of the apps
that actually moved.

## Feature registry (13-A, T13.0.1–6)

### P13-D3 — Typed Python registry (`rag_app.features`)

One `FeatureEntry` dataclass per shipped feature: `id`, `name`, `phase`, `status` (`enabled`
/ `disabled` / `retained-not-adopted` / `expected-fail`), an optional **health probe**
(`≤ 2 s`, enforced by `run_health_probe`'s hard deadline — a thread-pool future with
`.result(timeout=2.0)`, never a probe that can block the caller past it), the Prometheus
metric names it claims, THREAT_MODEL catalogue IDs (empty until 13-D's defence lab),
quality-probe ids, a docs/ADR link, `dev_only`/`azure_only` flags. 37 entries cover every
shipped feature as of 13-A (Stack, RAG, Quality, Identity, Abuse, Privacy, Browser security,
one Azure-only entry, and the `/admin` placeholder) — read from the real code, not invented;
`admin-api` only covers `/admin/ping` today (12b's placeholder) since 13.1's real admin
routes are a later block.

**Two probe kinds, two budgets.** A health probe answers in well under 2 s EVEN WITH NO LIVE
STACK AT ALL (the normal case for the fast gate and for CI): Postgres/migrations probes use
a short `connect_timeout` and report `down` gracefully on any failure; embeddings/reranker/
LLM-provider probes are **configuration-only** checks (never a real embed/rerank/generation
call — those cost real seconds of model load/inference, or real Azure OpenAI tokens, which
the task explicitly rules out for a health probe). A quality probe (today: one,
`quality_probe_misspelling`) measures actual product behaviour and needs a live DB/Ollama;
with none reachable it reports `skipped`, never a failure — the `registry` gate step only
requires it to resolve ("answer"), not to succeed, whenever the stack genuinely isn't there.

**The misspelling probe (`quality-misspelling-probe`, `status="expected-fail"`)**
demonstrates the Phase 14 gap directly rather than merely asserting it: it misspells one word
of a real golden-set question and runs it through `rag_app.retrieval.hybrid_search` — the
SAME path production uses — checking whether the expected document is still retrieved. With
no live DB (the fast gate's normal case) it reports `skipped`; whenever `--full`/`--only
registry,...` has a reachable gate DB, the `registry` step requires it to report `failed`
(not `passed`) — a `passed` result there is itself a FAIL, forcing a registry update the day
Phase 14 actually fixes this, rather than letting the registry silently drift stale.

### `registry` gate step (T13.0.5)

A consistent `tags=` on every FastAPI route (`stack` / `rag` / `identity` / `admin` /
`browser-security` / `privacy`) lets `check_registry(app, prometheus_registry)` cross-check
the REAL running app and the REAL Prometheus registry against the REGISTRY: every route tag
must be claimed by some entry's `router_tags`; every declared metric name must exist in the
Prometheus registry (base name, or `_total` for a counter — `prometheus_client`'s own
`collect()` reports a counter's FAMILY name without the suffix, while every entry here names
the real public `/metrics` line); every module an entry names must import; an `expected-fail`
entry must name a registered quality probe. `rag_app.devtools.registry_check` (the
`registry` gate step, fast tier — cheap even with no stack) runs this plus every health probe
plus the misspelling probe. One FastAPI implementation detail this module had to account
for: a newer FastAPI wraps an `include_router()`'d router lazily as a single
`_IncludedRouter` object in `app.router.routes` instead of inlining its routes there — its
real routes live on `.original_router.routes`; `rag_app.features.iter_routes` flattens this
for both `route_probe` and `check_registry`.

**Fix (DA-13-A-2, block 13-A-F):** the tag-claim check above only ever looked at tags that
EXIST (`used_tags - claimed_tags`) — a route added with no `tags=` at all contributed
nothing to `used_tags` and passed vacuously, so "a new router without an entry fails the
gate" was not actually true for that case. `check_registry` now also asserts every
`APIRoute` `iter_routes` finds declares at least one tag, independent of whether anything
claims it (framework routes like `/docs` are plain Starlette `Route`s with no tags concept
and stay excluded, same as the existing check).

### Missing metrics (T13.0.6, DEC-028 A5, D-ER)

Only five Prometheus metrics existed before this block. Added, every label a small bounded
vocabulary (never a user id, e-mail or IP — the same reasoning `CLIENT_IP_SIGNATURE_TOTAL`
already established): `secrag_rate_limit_hits_total{route_group}` (chat / login / email /
csp_report / account / mfa — every `rag_app.api.deps`/`rag_app.api.admin` rate-limit
dependency's own 429 branch); `secrag_lockout_hits_total` (a login blocked by an active
lockout); `secrag_signup_blocked_total{reason}` (disposable_email / signup_velocity);
`secrag_csp_reports_total` (one per logged CSP violation entry); `secrag_mfa_step_up_total
{result}` (success / failure); `secrag_erasure_pending` / `secrag_erasure_oldest_age_seconds`
/ `secrag_erasure_failures_total` (D-ER).

**Behaviour tests (DA-13-A-3, block 13-A-F):** `secrag_lockout_hits_total` and
`secrag_mfa_step_up_total` were wired at their real call sites but only ever exercised by
calling the metric directly (`test_metrics.py`'s label-shape checks). `test_lockout.py`
and `test_mfa.py` now each drive the real route (`POST /auth/login` past the lockout
threshold; `POST /account/mfa/step-up` with a wrong and a valid code) and assert the
counter increments by exactly 1 — DB-marked, run in `--full`/the `security-suite` gate
step.

**Known limitation of the three erasure metrics (for 13-B/13-C):** Prometheus state is
per-process. `rag_app.erasure.refresh_erasure_gauges` is called right after
`request_erasure` commits (in the API process — scraped correctly) and from
`Purger._count_open`/`Purger._mark` (the purger's OWN process — a separate Container Apps
Job on Azure, a separate compose service locally — never scraped from the API's `/metrics`
port). A continuously fresh value with no triggering API-process event (e.g. a tombstone
that has simply been open for a day, with nothing else happening meanwhile) needs either a
periodic in-process refresh or a scrape-time custom collector; the dashboard's own polling
tick (T13.4.1/T13.4.2) is the natural place to add it once that mechanism exists.

## Live updates + metrics backends (13-C, T13.4.1–T13.4.3, R6-4)

**Decision: mode is decided server-side, never a frontend build-time variable.** Locally
the dashboard holds open one SSE connection (`GET /admin/stream`, T13.4.1): an immediate
"snapshot", then a "heartbeat" every tick, re-checking auth and that MFA is set on the
session (not recent — DA-13-C-5, there is no step-up TTL) WITHOUT refreshing
`last_seen_at` (a background admin tab must still idle out on schedule), idle-closing after
a bound regardless, and capping concurrent connections. On Azure the dashboard instead
polls at a LOW frequency plus a manual "Refresh now" button (T13.4.2) — Container Apps'
**internal ingress + scale-to-zero** (TF16) make a long-lived, always-open SSE connection
exactly the class of resource a scale-to-zero replica should not hold: it would pin the
replica awake, fight the platform's own idle-scale-down, and (being internal-ingress-only)
gains nothing a low-frequency poll through the frontend's own BFF does not already give an
admin. `ADMIN_LIVE_MODE` (`config.py`) is the one flag that decides which: default
`"sse"` is right for every local/compose/CI context, so it must be set explicitly to
`"polling"` on the live Azure Container App (an explicit required-env-var, never left to
an implicit "Azure-safe default" — unlike most other Azure-vs-local settings in this
project).

**Decision: why no managed Prometheus on Azure (R6-4).** Azure Monitor managed
Prometheus/Grafana (or a self-run Prometheus container/sidecar) is real extra cost and
operational surface — another resource to provision, secure and keep patched — for a
single-replica, scale-to-zero demo app where "this replica's current numbers" is already
enough signal for an admin to act on; the $86 student-credit budget (DEBT-021) has no room
for it either. Decision: **no managed Prometheus, no sidecar, on Azure** — `rag_app.
metrics_backend.MetricsBackend` has two implementations instead, contract-tested to return
the exact same shape for the same input (`test_metrics_backend.py::
test_both_backends_return_the_same_shape_for_the_same_names`):

- **local** (`PrometheusMetricsBackend`): one instant PromQL query per metric against the
  compose `prometheus` container (history available — the local dashboard's latency panel
  can show real p50/p95 trends).
- **Azure** (`InProcessMetricsBackend`): reads the CURRENT value straight out of THIS
  replica's own `prometheus_client` registry — the exact numbers its internal `/metrics`
  port would otherwise expose, with no network call and no sidecar. **History is never
  available this way** (only ever "now"; a scale-to-zero restart resets every counter to
  0) — the dashboard shows "Azure: n/a" for it (frontend concern only; the redesigned shell,
  13-G2, moved this note from a standalone panel into the header's own live-mode line,
  `admin-history-note`, shown only when `live_mode` is `"polling"`).

**Consequence.** Every Azure metric value on the dashboard — including the T13.5.2 banner
alerts below, which read the SAME `MetricsBackend` — is "this replica, since it last
started" only, never cross-replica or historical; a lockout spike or an email-failure count
can reset to 0 on a cold start that happened moments before an admin looks. Accepted:
catching most real incidents (an admin who looks reasonably often) is the actual goal, not
a complete audit trail (that already lives in `admin_actions`/structured logs, 15.3).
Re-evaluate only if Azure spend genuinely allows a managed option later (not planned).

**Banner alerts (T13.5.2, R6-1, P13-D4).** `GET /admin/features` (its `alerts` field — what
the dashboard's polling tick reads, one probe sweep per tick, DA-13-E-2), the stand-alone
`GET /admin/alerts` and the SSE snapshot/
heartbeat's own `alerts` field share ONE implementation (`rag_app.api.admin.
_banner_alerts`) over the SAME five conditions regardless of live mode: an open erasure
request past 24 h (amber past 20 h, D-ER's own promise); a lockout-hit count past a fixed,
documented floor (an absolute count, never a rate — no history on Azure to compute a rate
from, same R6-4 reasoning); any registry entry's health probe reporting `down` whose
feature is `expected_here` (T13.8.9, DEBT-053 — a Settings-derived bool, e.g.
`admin-defence-lab`'s `lab_enabled and env != "prod"`; a `dev_only`/`azure_only` entry
that is NOT `expected_here` reporting `down` is the EXPECTED signal of a feature switched
off here by design, e.g. the defence lab locally off or an Azure-only check run
off-Azure — excluded, never a false alarm, while the SAME entry turned ON here and still
`down` now alerts); any email send failure;
and the daily answer cap (R6-1) reached, read directly from `usage_daily` (never a
Prometheus metric — the counter is per-UTC-day, not per-process, so an in-process-only
Azure replica would otherwise show a wrong, reset-on-restart value for THIS one condition).

## Defence lab (13-D, T13.3.1–T13.3.6, local only — DEC-028)

**LAB_ENABLED (T13.3.1).** A plain `dev_only_flag` field, same mechanism every other
development-only feature in `rag_app.config` uses: off by default, the start-up guard
refuses to serve at all with `ENV=prod` whatever the setting says. `create_app` registers
`rag_app.api.lab`'s router only when the flag is on, so the lab's routes are genuinely
ABSENT with it off — a 404, not a 403.

**Probe framework (T13.3.2/T13.3.3).** Rather than duplicating the assertion a
security-suite test already makes ("one implementation" — the auth/CSRF/session probes
specifically must reuse the existing security-suite cases, not re-type their logic),
`rag_app.defence_lab` keys every probe to a real
`docs/security/THREAT_MODEL.md` catalogue id and re-runs that entry's own Test ID through
ONE batched `pytest -v` subprocess (the pyproject-wide `addopts = "-q"` had to be overridden
per invocation, or no per-test result lines are printed at all). A parametrized test
(REC-09) selected by its bare node id runs every variant — the parser matches `nodeid` or
`nodeid[...]` and requires every matched variant to pass, never partial credit.
`CATALOGUE_PROBE_IDS` curates 19 ids across the Phase 11–12 categories named in T13.3.3;
CSRF and server-side sessions had no catalogue id at all before this block (only prose in
section 4's STRIDE tables) — added as REC-11/REC-12 so the lab can address them by id.
Results persist into the existing `defence_runs` table (created in 0008, 13-B, specifically
for this block) via the CALLING session — a persistent history in whatever long-lived
database that is, independent of the catalogue probes' own disposable per-run test-DB-
harness database (`TEST_DATABASE_URL`, the "fixed local target": the gate project,
`127.0.0.1:15432`, same mechanism `security-suite`/`db-tests` already rely on).

**Erasure at scale (DEBT-005) stays its own gate step.** The erasure-at-scale probe is
deliberately NOT part of the catalogue-driven set: it needs a throwaway Postgres
SUPERUSER connection (`SCALE_ADMIN_URL`) only the gate project hands out, a different
operational shape from the admin-button/CLI path. `rag_app.devtools.erasure_scale`
(unchanged entry point) already killed a purger run mid-way and proved the next run
finishes — the one addition is the explicit DEBT-005 assertion: a fresh throwaway database
ends with exactly 2 `purger_runs` rows, 1 still unfinished (the killed run's own row,
`finished_at` forever `NULL` since `SIGKILL` bypasses its `finally` block) — both runs
visible, nothing lost or duplicated.

**Negative control (DEBT-017 part 2, DA-G1-7).** `rag_app.devtools.negative_control` is a
one-off, never-gated proof that the erasure-scale latency criterion has teeth: it patches a
THROWAWAY `git worktree`'s `DELETE /account` route to call the existing synchronous
`rag_app.erasure.erase_user` (today only used by the offline `key_recovery.sh` tool)
instead of the real asynchronous `request_erasure` path, then measures one big user's
erasure latency. The first real run (100,000 messages, the same size `erasure-scale` uses)
still passed the 200 ms criterion — Postgres' own FK `ON DELETE CASCADE` is a fast,
set-based operation, not the batched/checkpointed work the real purger does — scaling to
1,000,000 messages (`NEGCTL_MESSAGES`) reliably failed it (717 ms median, real run). The
worktree and its throwaway database are always removed, success or failure.

**Admin routes + gate step (T13.3.4/T13.3.5/T13.3.6).** `POST /admin/lab/run` (runs every
catalogue probe, records the results, logs an `admin_actions` row) and `GET /admin/lab/runs`
(history) follow the same `require_permission_and_mfa` + `rate_limit_account` + CSRF
conventions as the rest of `/admin/*` — "CLI + button, same code" (T13.3.5) refers
specifically to these catalogue probes, which are the only ones that make sense against a
live admin session; the erasure-scale/negative-control tools stay gate/devtools-only, for
the operational reasons above. The `defence-suite` gate step (T13.3.6) runs the same CLI
and fails unless 100% of probes match their expected outcome.

**Found running `security-suite` for real, fixed before relying on it (folded into this
block).** `rag_app.api.lab`'s module-level `route=` registrations populate
`authz.DECLARED_PERMISSION_ROUTES` at IMPORT time, independent of whether `create_app` ever
mounts the router. Once any test in a pytest session imports the module (as the lab's own
tests must), `test_route_walker.py`'s "walker discovery is vacuous" floor saw a route no
`create_app()` built with the flag off actually serves — a false positive the very first
dev-only conditionally-mounted feature in this codebase tripped. Fixed by intersecting the
declared routes against the CURRENT app's own mounted routes first; a regression test
proves the pre-fix formula really did flag a declared-but-unmounted route and the fix does
not. The same pattern will recur for Phase 16/19/20's own dev-only features (fake LLM
provider, data explorer, code-fix) — the fix is in the shared walker test, not specific to
the lab.

## Dashboard shell redesign (13-G2, T13.8.4–T13.8.6)

**Decision: an explicit, EXPLAINED status vocabulary, not a bare "N probe(s) not ok".**
`frontend/lib/adminTriage.ts` maps each feature's raw `probe_status` to one of four
triage states — OK, Warning, Critical, or "Off here" — reusing the vault palette's existing
semantic tokens (steel-light/amber/danger/steel-dark; there is no separate "success green"
token — **13-J below restores the mockup's dedicated tokens**) with an icon *and* text on
every badge, never colour alone. A `down` probe on a
`dev_only`/`azure_only` feature that is also NOT `expected_here` (T13.8.9, DEBT-053 — e.g.
`admin-defence-lab`'s `lab_enabled and env != "prod"`) is "Off here" with a truthful
one-line "why" (e.g. the defence lab being off on Azure by design) rather than counted as
a failure — the same `expected_here`-gated exclusion `_banner_alerts`' `probe_failures`
condition already applies server-side (T13.3.1 precedent, refined in 13-G3), now visible
in the UI instead of silently absent. Features sort worst
first (Critical, Warning, Off here, then OK); the triage list's status filters (All/
Critical/Warning/Off here/OK) and the per-feature side drawer (probe detail, its own
`metrics` map, related alerts via `AdminAlertOut.feature_ids` — T13.8.3a — and a `docs`
link, T13.8.6) replace the old `OverviewPanel`/`FeatureCards`/`SecurityPanel` trio: a
feature's security-relevant metrics (rate limiting, lockouts, CSP reports, …) now live in
that feature's own drawer rather than a second, separately-aggregated panel.

**Decision: no "MFA verified" badge in the header (reviewed, approved mockup change).**
Every `/admin/*` route already re-proves the session's MFA step-up server-side on every
request (`require_permission_and_mfa`); a header badge claiming "MFA verified" would be a
point-in-time client-side echo of that fact, not a new guarantee, and risks reading as a
promise the UI itself cannot keep if the step-up later expires mid-session (DA-13-C-5: no
step-up TTL is enforced client-side either). Reviewed and dropped from the approved mockup.

**Shell layout.** Header (title, live mode, last-refresh/polling text — the former
`admin-history-note`'s "Azure: n/a" caveat now lives here) + alert banner (severity badge,
jump action to the related feature via `feature_ids`, or to the Erasure section when none
resolves) + up to four KPI tiles (features healthy, active alerts, chat latency average
from `/admin/latency`'s existing `stages` "total" entry, erasure queue) + a left-hand
section menu (Triage · Pipeline · Quality · Erasure · Defence lab, the last one only when
`admin-defence-lab`'s own probe is `ok` — same gating `DefenceLabPanel` already used).
Pipeline/Quality/Erasure/Defence lab keep their existing panels verbatim inside the new
shell for this block; their own redesign is 13-G3.

## Atomic-step metrics + the Pipeline waterfall (13-G1, T13.8.1–T13.8.2; 13-G3, T13.8.7)

**Decision: a SECOND histogram, with its own fail-closed allow-list, not a labelled
extension of the existing stage histogram.** `secrag_chat_step_seconds` (new, T13.8.1)
records atomic steps WITHIN a UI pipeline stage — e.g. `(retrieving, embed_query)`,
`(generating, token_stream)` — as a separate `Histogram` from the pre-existing
`secrag_chat_stage_seconds`. The alternative (adding an optional `step` label to the
existing stage histogram) was rejected: it would let a future call site observe a stage
with an arbitrary step name (or none), leaving the no-high-cardinality-label discipline
resting entirely on code review. Instead, `metrics.STEP_ALLOWLIST` is a fixed, literal
`tuple[tuple[str, tuple[str, ...]], ...]` naming every legal `(stage, step)` pair in
pipeline order, and `observe_step` **raises** `UnknownPipelineStepError` for anything
outside it — a dev-time bug signal (every real call site passes a hard-coded literal pair,
so this can only fire from a typo or a new step someone forgot to add to the allow-list),
never a silently-dropped or silently-renamed observation. `timing.step()`'s own `finally`
re-raises the wrapped block's original exception even if `observe_step` ALSO raises while
recording (DA-13-G1-4) — a metrics bug must never mask a real pipeline error.

**Decision: a stage's waterfall total is the SUM of its own steps, never reconciled
against the separate `stages` field.** `/admin/latency`'s `pipeline` field groups every
`(stage, step)` pair by its UI pipeline stage (`metrics.STEP_ALLOWLIST`'s own grouping —
a DIFFERENT, higher-level grouping than `StageLatencyOut.stage`; e.g. "retrieving" covers
both the old "embed" and "hybrid_search" stage names). The DA-13-G1-1 review found three of
the six UI stages did NOT sum exactly to their `secrag_chat_stage_seconds` counterpart
(code that ran inside the stage span but outside any step's own `with` block — the
"untracked gap"); the 13-G1-fix (`012581d..0cfaddc`) closed the three known gaps by moving
`retrieval.py`'s ranked-list loop, `reranking.py`'s `order_by_scores` and
`generation.py`'s `parse_answer` (both answer paths) inside their stage's own `with
timing.stage(...)` span. Even so, the frontend's `adminPipeline.ts::buildPipelineWaterfall`
(13-G3) computes each stage's own total as `sum(step.avg_seconds for step in
stage.steps)` and NEVER reads `AdminLatencyResponse.stages` for that same stage name — a
future untracked gap (or a regression of this fix) must never make the waterfall's parent
bar and its own children visibly disagree; the two numbers are allowed to drift apart
without it being a UI bug, because they are deliberately two different (and both
legitimate) ways of totalling the same work.

**Waterfall semantics (13-G3).** One row per UI pipeline stage, in `STEP_ALLOWLIST`'s own
order, each expandable to its atomic steps; every step's bar starts where the previous
step's ends (`waterfallBarPercent`), so the stage row's own bar visually spans exactly its
children's combined width. The single globally slowest OBSERVED step (across every stage,
ties broken by pipeline order) is the only step highlighted — never a per-stage "slowest",
which would highlight something in every stage regardless of how fast the whole pipeline
actually is. The chit-chat fast path's own stage (`responding` → `stream_canned_reply`) is
a SEPARATE pipeline from the RAG path (classifying → retrieving → reranking → generating →
checking): its bar is drawn starting at 0, not continuing the RAG path's cumulative offset,
and it is excluded from the RAG path's own total average — summing durations from two
different code paths into one number would not mean anything. An unobserved `(stage,
step)` pair (nothing has exercised it yet this process) still appears, with `count=0`,
`avg_seconds=None` and a visible "n/a" — the waterfall's rows never shuffle or disappear as
traffic arrives, same no-history/R6-4 caveat as every other metric on this dashboard.
Non-streaming answers never record a `token_stream` step (there is nothing to stream); a
third, less obvious case is a mid-stream client disconnect for a STREAMING answer, where
`GeneratorExit` can unwind past the `token_stream` step's own recording — the 13-G1-fix
closed that one too (the duration is now recorded in a `finally`, so the partial time is
still captured rather than silently dropped). Per-request latency TRACES (as opposed to
this replica-wide average) are out of scope here — that is 16-F's own future work; the
Pipeline section carries a small caption saying so rather than implying it already exists.

## Wake-up UX after scale-to-zero (13-H, T13.9.1–T13.9.2, DEBT-056, DEC-048)

**Why.** Every Container App runs `minReplicas 0`/`cooldownPeriod 300 s` (since 11a): after
~5 min idle, Azure scales to zero and the next request hits a cold start. The session
itself is fine (Postgres-backed), but without this block the page just looked dead/broken
for the length of the cold start.

**P13-D-wakeup — classification, not a timer.** The frontend never guesses "is the backend
cold" from elapsed time; it classifies exactly what `lib/bffProxy.ts` already returns for a
failed upstream call: a connect-level failure (502 `"upstream unreachable"`, thrown before
`fetch` to the backend ever got a response — no replica is listening yet) vs. a BFF timeout
(502 `"upstream timed out"`, `bffConfig.ts`'s `MIN_TIMEOUT_MS` abort, DEC-006) vs. any other
pass-through 502/503/504 vs. a real 4xx. This needed **no backend change at all** — the
whole policy lives in `frontend/lib/wakeUp.ts`, reading signals the BFF already produced for
other reasons (DEC-006's own timeout, `bffProxy.ts`'s own catch block).

**Idempotency — the "don't answer/charge a chat question twice" requirement.** The first
class above (connect-level failure) is the classic scale-from-zero signature and is, by
construction, provably safe: the request never reached the backend, whatever HTTP method it
was, so it is retried automatically with bounded exponential backoff (60 s total budget).
The other three failure classes are ambiguous for a non-idempotent call (the backend may
already be mid-flight) — those are retried automatically only when the caller marks the call
`idempotent` (every GET, by the HTTP method alone, plus `login` explicitly, since repeating
a login has no harmful side effect); every other case (a chat question, register, an admin
mutation) surfaces once as a `WakeUpError` with **no automatic resend** — the UI shows a
Retry button instead, so at most one further attempt ever happens, and only on the user's
own explicit click. `frontend/lib/api.ts`'s `request()`/`streamChat()` are the only two call
sites that implement this; `frontend/lib/useWakeState.ts` + `frontend/components/
WakeBanner.tsx` (`aria-live="polite"`, reusing the existing `--amber`/`--danger-text`
tokens) are the one shared UI piece every page (login, chat, the admin dashboard's data
loads) wires the same three hooks (`onWaking`/`onRecovered`/`onWakeFailed`) into, rather than
a per-page copy.

**Found running this for real (T13.9.2): `app/admin/page.tsx`'s own SSR gate.** Before this
block, a cold-start-shaped failure on the dashboard's first (server-side) load was
indistinguishable from "not an admin" and showed the exact same "you do not have access to
this page" message — a 4xx-shaped lie about a user who may well be an admin. The same
`classifyWakeSignal` now runs there too (no retry loop server-side, just the right message +
a reload link), closing that gap as part of the same fix rather than leaving it half done.

**Scope boundary (DEC-048, orchestrator decision, not this block's).** Whether to also raise
`cooldownPeriod` on Azure (300 s → 900/1800 s) is a cost trade-off the orchestrator brings to
the user separately (T13.9.3); this block's own Done-when is the Local UX only — the
eventual Azure gate (T13.9.4, ≥ 6 min idle → first action shows the wake-up state and then
succeeds) is a later promotion step.

## 13-J: mockup palette restored

**Why.** 13-G2/13-G3 shipped the redesigned shell reusing the generic vault tokens
(`--steel-light`/`--amber`/`--danger`/`--steel-dark`) for status, because at the time there
was "no dedicated success green token" — the live dashboard read grey/amber only, never the
approved mockup's (`Main.dc.html`, approved 2026-11-19) green/amber/salmon/blue-grey status
pills with tinted backgrounds. This block restores that palette **without** adopting the
mockup's IBM Plex fonts (cohesion with the rest of the app, which stays monospace
throughout, per the orchestrator's explicit brief).

**Decision: new tokens, not a repaint of the existing ones.** `app/globals.css` gained 16
new custom properties, scoped to the admin dashboard only — `--ok`/`--ok-bg`/`--ok-border`,
`--warn`/`--warn-bg`/`--warn-border` (`--warn` aliases `--amber-bright` via
`var(--amber-bright)` — same value, one source of truth), `--crit`/`--crit-bg`/
`--crit-border`, `--off`/`--off-bg`/`--off-border`, plus `--surface`/`--surface-raised`/
`--line`/`--text-strong`/`--text-muted` for the mockup's card/drawer backgrounds and
heading/secondary text roles — mirrored in `tailwind.config.ts` as `colors.vault.{ok,warn,
crit,off}{,-bg,-border}` / `surface`/`surface-raised`/`line`/`text-strong`/`text-muted`.
Chat/login/404 are untouched — they keep the pre-existing vault tokens.

**`lib/adminColours.ts` (C5 pattern, same as `adminTriage.ts`).** `TONE_CLASS`/
`TONE_TEXT_CLASS` are StatusBadge's and every panel's single source of truth for the four
tones' Tailwind classes — unit-tested directly (`lib/adminColours.test.ts`), rather than
inlined per-component, so a future tone change cannot drift between StatusBadge and the
triage filter chips/alert banner/erasure countdown that all read the SAME map.

**WCAG AA verified, not assumed.** The same module implements the WCAG contrast maths
(relative luminance → contrast ratio, alpha-compositing a tinted pill's rgba background over
the two real surfaces a pill can sit on — the plain card `--surface` and the raised feature
drawer `--surface-raised`) and a test asserts every tone's text clears 4.5:1 against its own
composited background: ok 8.4–9.9:1, warn 7.9–9.2:1, crit 6.6–7.6:1, off 7.9–9.2:1 (worst
case first). None of the mockup's four status colours failed AA — nothing had to be
changed from the mockup's own values.

**Filter chips keep the mockup's own nuance: tone colours the TEXT, amber colours the
SELECTION.** `Main.dc.html`'s filter-chip `style()` colours each chip's label with its own
status tone (a Critical chip's label is always salmon) regardless of whether it is the
active filter; only the border/background switch to amber when a chip IS the active filter.
`TriagePanel.tsx` now matches this exactly, rather than the previous "active chip turns
amber, inactive chips are all the same grey" behaviour.

**KPI tile sub-lines stay data-driven.** The mockup's four KPI tiles each have one fixed
sub-line colour (crit/warn/muted/warn) in the static sample data; `AdminDashboard.tsx`
instead derives each sub-line's colour from the SAME real counts the sub-line text already
reads (`health.critical`/`severity.red`/`queue.overdue`, falling back to warn, then muted)
— the mockup's intent (draw the eye to a bad number) without inventing a colour that
disagrees with the real number next to it.

**Found while wiring this up: Tailwind's `content` glob never scanned `lib/`.**
`tailwind.config.ts`'s `content` array only listed `app/**` and `components/**`; with the
tone classes defined as string literals in `lib/adminColours.ts`, Tailwind's JIT purged
them as unused, so the built CSS had no rule for e.g. `bg-vault-ok-bg` at all — the OK pill
rendered with a fully transparent background in a real build (caught by the new e2e smoke
test asserting the OK badge's computed `background-color`, not by typecheck/lint/unit,
which cannot see generated CSS). Fixed by adding `./lib/**/*.{js,ts,jsx,tsx,mdx}` to
`content`.

**Known gap for the DA (e2e tone coverage).** The e2e smoke
(`e2e/admin-dashboard.spec.ts`) can only exercise the OK tone live — a freshly seeded e2e
stack has no Critical/Warning feature and no alerts (T13.5.2's own comment), and fabricating
one would need a backend test hook or behaviour change, out of this block's scope ("do not
change data/polling/behaviour"). Warn/crit/off are proven correct at the unit level
(`lib/adminColours.test.ts`) and visually in a manual run against the mockup, not live
end-to-end. A follow-up phase could add an admin-only "force a probe status" test
hook if full-tone e2e coverage becomes a priority.

**Minor approximation (reported, not silently changed) — SUPERSEDED, see 13-J3 below.** The
mockup actually uses THREE border/divider shades (`#2b2f35` card border, `#23262b` row-level
divider, `#454c56` drawer/button border); this block introduces one new token, `--line`
(`#2b2f35`), for both card borders and row dividers, rather than a fourth near-identical shade
for the `#23262b` case. The visual difference is negligible (both are dark neutral greys a
few % apart) and borders are not subject to WCAG text-contrast rules. **In practice the actual
implementation went further than this note describes, applying a `/60`/`/70` opacity modifier
to `--line` for row/stage dividers to approximate the lighter `#23262b`/`#1d2025` shades — an
instance of the exact same `var()`-opacity-modifier bug 13-J3 found and fixed (below), so those
dividers rendered with no line at all, not merely an approximate colour.**

## 13-J2: wake banner on slow (held) responses + DA-13-J-1 radius fix (DEBT-060)

**DA-13-J-1 fix — card/panel radius now matches the mockup.** 13-J's own colour work left the
radius untouched: `TriagePanel`/`ErasurePanel`/`LatencyPanel`/`QualityPanel`/
`DefenceLabPanel`/`AdminDashboard`'s card wrappers used Tailwind's own `rounded-lg` default
(8px), not the mockup's 10px (`Main.dc.html` lines 59/70/86/120/166/215/260/305); the
defence-lab inner probe card and several action buttons used plain `rounded` (4px), not the
mockup's 8px/6px. Fixed with three named, admin-scope `tailwind.config.ts` tokens
(`rounded-admin-card` 10px, `rounded-admin-card-inner` 8px, `rounded-admin-btn` 6px) applied
across the DA's six flagged files, plus the KPI tile/nav sidebar/alert card (the same visual
card family, not individually named by the DA but kept consistent rather than left at the old
8px next to the newly-10px panels). `FeatureDrawer.tsx`'s own `rounded-lg` is deliberately
left as is — outside the DA's reviewed file list; a future DA pass can decide whether the
mockup's drawer radius differs. Proven by a new e2e assertion of the real computed
`border-radius` on the triage panel card and a nav button (`e2e/admin-dashboard.spec.ts`).

**13-J2 (T13.11.5/T13.11.6, user 2026-11-21 01:15) — the gap the 13-H retry policy cannot
close.** `lib/wakeUp.ts`'s retry policy (13-H, above) only has an opinion once a BFF call has
*failed*. Proven locally by the orchestrator the same day, with the real `next start` build
and fake backends: a backend that REFUSES the connection shows the banner after 3 s (the
13-H path, correctly); a backend that merely HOLDS the request for 20 s — exactly what the
Container Apps activator does during a real scale-from-zero, queuing the request instead of
answering with any error — shows nothing at all, just the button's own busy "…". This is what
the user actually saw on Azure after a quiet period.

**Design: a separate, purely-visual timer, never a second retry mechanism.** `frontend/lib/
pendingWake.ts`'s `createPendingWakeTimer` is a one-shot timer (injectable `setTimeout`/
`clearTimeout`, so `node --test` exercises it with no real delay) that calls `onPending` if a
call is still pending `PENDING_WAKE_DELAY_MS` (3 s, one named constant) later, and `onCleared`
the instant `clear()` is called after it already fired. It never touches the network, never
retries, never resends — it is purely a "should the user be told this is taking a while"
signal, bracketed around the exact same call the page would have made anyway.
`frontend/lib/useWakeState.ts` owns one `createPendingWakeTimer` instance per page alongside
the existing 13-H retry state, and combines the two into the single state `<WakeBanner>`
renders via `combinePendingWakeState()`: the retry state (`"waking"`/`"failed"`) ALWAYS wins,
so a real 502-shaped retry already showing its own banner is never masked or duplicated by
the visual timer — there is exactly one banner on screen, ever (T13.11.6's "no double
banner" case). Unmount cleanup calls the timer's `dispose()`, never `clear()`, so a
still-armed timer is cancelled WITHOUT calling `onCleared` (which would be a `setState`
after unmount).

**What "pending" covers, per call site.** Login and the admin dashboard's initial load treat
the WHOLE call (including any 13-H retries inside it) as pending, cleared only once it
settles — `wake.startPending()`/`wake.clearPending()` wrap the single `await`. Chat
send/stream is different, per the Done-when's own wording ("until the first byte/token
arrives"): a streaming answer can legitimately keep going for several more seconds after the
first byte without needing the banner, so `clearPending()` is called from `onConversation`/
`onStage`/`onToken` (whichever the backend's SSE stream dispatches first — `generation.py`'s
`answer_question_stream` always yields `stage: "classifying"` as its very first event, before
any retrieval/generation work, so this fires within milliseconds of the connection
succeeding) and defensively again from `onDone`/`onError`/`onWakeFailed`. The admin
dashboard's own polling interval and "Refresh now" button are deliberately NOT wired to this
timer — out of scope ("admin dashboard **loads**", and the user's own standing "do not touch
polling" constraint from 13-J).

**Known boundary: the server-rendered first `/admin` load (DA-13-J2-1).** `app/admin/page.tsx`
is server-rendered, so on a fresh or direct `/admin` navigation during a HELD cold start no
client-side timer can run before hydration: the user sees only the browser's own native
loading indicator, never the wake banner. The banner covers every client-side call (login,
chat send/stream, the dashboard's client-side initial load); a global navigation-loading
indicator for the SSR case is a possible future admin-UX item (DA-13-J2-2), not built here.

**Tests.** `lib/pendingWake.test.ts` (12 cases): never fires before `clear()` cancels it
(the negative-control shape), fires exactly once at the threshold, `clear()`/`dispose()`
after firing (the latter silent, no `onCleared`), re-arming for a second call, the default
delay, and `combinePendingWakeState`'s precedence rules (retry state always wins, including
the "given-up `failed` is never downgraded back to `waking`" case). `e2e/wakeup-slow.spec.ts`:
a `/api/chat/stream` route delayed ~6.5 s (never fulfilled with a fake response — the real
request is let through unmodified, so the eventual answer is the real backend's own) proves
the banner is absent before ~1.5 s, visible by ~4 s with no Retry button, and gone the moment
the real answer streams in, exactly once; a negative control proves a normal-speed chat turn
never shows it at all.

## 13-J3: colours, second pass (T13.12.1–T13.12.4, DEBT-059 reopened, DEBT-062)

**Why reopened.** 13-J/13-J2 were promoted (`442d942`), but the user's own smoke test called
the live dashboard "mucho menos colorido" than the approved mockup. The orchestrator traced
this to a genuine bug, not a subjective styling gap: several of 13-J's own tinted-background
classes compiled to **no CSS rule at all**.

**Root cause: Tailwind 3 cannot apply an opacity modifier to a `var()` colour.** A Tailwind
colour defined as a CSS custom property (`tailwind.config.ts`: `vault.amber: "var(--amber)"`,
`vault.line: "var(--line)"`) has no literal RGB triplet for Tailwind's JIT compiler to scale
when a class asks for a fractional opacity (`bg-vault-amber/10`, `border-vault-line/60`) — it
silently emits nothing, with no build warning or error. Confirmed by the orchestrator
compiling the admin sources directly with the Tailwind CLI. This had THREE live
consequences, not just the one first noticed:
- `TriagePanel.tsx`'s selected row and `AdminDashboard.tsx`'s active nav item both used
  `bg-vault-amber/10` — no tint at all (should have been 0.10 and 0.12 respectively, two
  DIFFERENT values the mockup actually uses).
- `TriagePanel.tsx`'s pressed filter chip used the same `bg-vault-amber/10` (should have been
  0.14).
- Grepping every admin component for the same shape (`/<n>` after any `vault-*` colour) found
  it ALSO broke every row/metrics/pipeline-stage divider: `TriagePanel`/`FeatureDrawer`/
  `ErasurePanel`'s `border-vault-line/60` and `LatencyPanel`'s `border-vault-line/70` — these
  had rendered with NO divider line at all since 13-G2/13-G3, undetected because 13-J's own
  ADR note ("Minor approximation" above) had assumed a single, slightly-transparent `--line`
  token was an acceptable stand-in for the mockup's separate `#23262b`/`#1d2025` shades — the
  actual code never even achieved that approximation; it rendered nothing.

**Rule going forward (enforced, not just documented): no class in any admin component may
ever apply an opacity modifier to a `vault-*` colour.** Every tint now has its own named,
pre-composited token instead — `--amber-wash-10`/`-12`/`-14` (selected row / active nav /
pressed chip) and `--row-divider`/`--stage-divider` (`#23262b`/`#1d2025`, the mockup's own
literal divider shades, replacing the single-token approximation 13-J shipped with) — mirrored
in `tailwind.config.ts`. `lib/adminTailwindCompile.test.ts` enforces the rule two ways: a
static grep that fails the moment any admin `.tsx` file writes a `vault-*` class with a
trailing `/<digits>`, and an actual Tailwind compile (via the `postcss`/`tailwindcss`
packages, in-process) of every `vault-*` class those files use, asserting each one produces a
real CSS rule — with a negative control that recompiles the original `bg-vault-amber/10`
fixture and asserts it produces none, proving the compile-check method would truly have
caught this. Both halves of the test fail on the pre-T13.12.1 code.

**DA-13-J-1's nav radius correction.** 13-J2's own radius fix (above) applied the generic
6px `rounded-admin-btn` to the left-hand section nav item; re-reading `Main.dc.html`'s
`sections[].style` directly shows the nav item has its OWN 8px radius, distinct from every
other admin button's 6px. Given its own named token, `rounded-admin-nav` (8px), and a 44px
min-height (`sections[].style`: `min-height: 44px`) the nav item never had before. The e2e
radius assertion for `admin-nav-pipeline` is updated from the (incorrect) 6px to 8px.

**Remaining T13.12.2 gaps closed in the same pass:** the header's target/radar SVG icon (28px,
amber stroke, `aria-hidden`, absent entirely before this block); filter-chip/row sizing
(36px/52px min-height, the mockup's own padding) and the row name's 500 font-weight; the
feature drawer's radius (`rounded-lg`'s 8px default → `rounded-admin-card`'s 10px — left
unfixed by 13-J2 as "outside the DA's reviewed file list"); the page's 1440px max-width
(`max-w-6xl`'s 1152px never matched it); KPI tile padding (16/18px); the alert banner's
padding (10/14px); and every secondary button (Refresh now, Expand all/Stages only, Run
probe/Show history, Open feature/Open erasure, Retry) restyled to the mockup's one shared
secondary-button look (6px radius, amber-bright text, `#454c56` border, 36px min-height) —
"Purge now" alone gets the mockup's OTHER, filled-amber primary button style.

**The erasure KPI sub-line was silently dropping information.** The mockup's "Erasure queue"
tile always shows BOTH clauses ("0 overdue · 1 near deadline"); the pre-13-J3 code only ever
rendered "`N` overdue", so a queue with nothing overdue yet but something about to be showed
no tint and no mention of it at all. `lib/adminKpi.ts`'s `erasureQueueSummary` now also
counts `nearDeadline` (the same `AMBER_THRESHOLD_SECONDS` `ErasurePanel.tsx`'s own countdown
colouring already used, re-exported from `adminKpi.ts` so the two thresholds cannot drift
apart), and the sub-line's tone follows it (crit if overdue, else warn if near-deadline, else
muted) — matching the mockup's own tone-when-non-zero rule exactly.

**Visual evidence (T13.12.3).** `e2e/admin-dashboard.spec.ts`'s real-stack test now asserts
the ACTUAL computed `background-color` of the selected row, the active nav item and the
pressed filter chip (all three fail on the pre-fix code — no background at all). A freshly
seeded e2e stack cannot naturally produce a critical/warning/off feature, two alerts or a
near-deadline erasure request all at once; a second test mocks the backend's own SSE snapshot
frame (`event: snapshot\ndata: {...}`, the exact shape `rag_app.api.admin._sse_frame`/
`lib/adminStream.ts` already define) plus the three REST calls the dashboard's first-load
effect fetches separately, reusing the already-enrolled e2e admin account rather than adding
a new seeded one. Screenshots of every section were taken against this seeded state for
side-by-side comparison with `Main.dc.html`.

**Fix block (user screenshot review, 2026-11-22): three more issues.**

1. **Filter chip/count text rendered glued together ("All38", not "All 38").** The mockup's
   own markup (`{{f.label}} <span>{{f.count}}</span>`) has a real space character between the
   label and the count. The first attempt at T13.12.2's chip restyle put `display:flex` on
   the button for `min-h-9`/`items-center` — but a flex container drops a whitespace-only
   anonymous box between flex items, so the literal JSX space collapsed, rendering "All38"/
   "Critical1" (same bug shape proven by a first (wrong) fix attempt: replacing the two text
   nodes with two flex children + `gap-1` gives a CSS layout gap, not a real text-content
   space — still "All38" to `textContent`/assistive tech/copy-paste, caught by a new e2e
   `toHaveText(/^All \d+$/)` assertion). The mockup's own filter-chip style string never sets
   `display:flex` either — fixed by dropping `flex`/`items-center` entirely and keeping the
   mockup's own literal `{{f.label}} <span>` markup (a real space), `min-h-9` alone still
   enforcing the 36px minimum height.
2. **Feature drawer's "Docs: …" line removed (user decision, overrides the mockup).** The
   user: its sample value (a private-runbook anchor link) "no sirve para nada" (not clickable,
   not useful in this drawer) — removed entirely from `FeatureDrawer.tsx`. `FeatureHealthOut.docs`
   itself is untouched (still a real backend field, used nowhere else in the frontend) — presentation
   only, no API change.
3. **Quality/Erasure/Defence-lab tables/cards matched to `Main.dc.html` exactly (user: "not a
   new debt", done in this same fix block rather than left as the follow-up the first pass
   reported).** `QualityPanel.tsx`: a header-level tone pill ("Passed"/"Regressed", was
   missing — only the per-row State pill existed before), the table's own `<th>`/`<td>`
   padding (8/6px) and divider colours (`--line` header border, `--row-divider` row border),
   right-aligned numeric columns, and the mockup's own dashed empty-state box (border-dashed,
   8px radius, 14px padding) replacing the plain empty-state paragraph. `ErasurePanel.tsx`:
   renamed header to "ERASURE REQUESTS · 24 H SLA" + the "Request IDs only" subtitle, a new
   "Rows left" column (summed from the real `rows_remaining` map the backend already returns,
   previously unused in the UI — replaces the old "Last error" column the mockup never shows;
   `last_error_class` stays a real wire field, just not its own column here any more), a
   visually-hidden ("Actions") header cell matching the mockup's own accessible-but-invisible
   technique, and the mockup's own closing paragraph ("Done rows show when they finished…").
   `DefenceLabPanel.tsx`: the mockup's own fluid `grid-template-columns:
   repeat(auto-fit,minmax(260px,1fr))` (was fixed Tailwind breakpoints), `gap-2.5`/`p-3.5`
   padding, 600-weight probe name (kept `font-mono` regardless — the monospace-everywhere
   exception; no separate human-readable probe name exists on the wire, `probe_id` is it),
   and a per-card "Run probe" button — the real backend has no per-probe run route
   (`adminLabRun()` always runs the whole lab), so each card's button wires to the SAME
   existing global action, exposed per-card to match the mockup's layout only, no new
   behaviour. The screenshot test's own fixture also flips `admin-defence-lab` to "ok" (so
   the tab is reachable for its own screenshot) and introduces `azure-internal-ingress` as
   the scenario's "off" example instead (fixture-only — the real backend's
   `azure-internal-ingress` entry has no health probe at all and would never be "down" for
   real, see `features.py`).

No remaining known gaps in these three sections after this fix block.

## 13-J3b: the real cold-start pipeline inside the chat wake banner (T13.12.5–T13.12.7, DEBT-062)

**Problem.** 13-J2's purely-visual pending timer (`lib/pendingWake.ts`) clears the chat wake
banner on the FIRST SSE event the stream produces — `{"type":"stage","stage":"classifying"}`,
which arrives almost instantly even on a true Azure cold start (the backend container is
already up and running by the time it can emit anything at all). The genuinely slow part —
the embedding call, which on Azure wakes its OWN scaled-to-zero Ollama Container App and
loads `bge-m3` (admin KPI: "slowest stage: embed", ~19.7 s avg) — happens strictly AFTER that
first event, inside the "retrieving" stage. So the banner disappeared right as the real wait
began, leaving the user staring at nothing for ~20 s.

**Backend: real sub-step events, not a timing guess (T13.12.5).** `rag_app.retrieval.hybrid_search`
always opened `timing.stage("embed")`/`timing.step("retrieving", "embed_query")` around the
embed call itself, but a plain callback passed into it cannot make
`generation.answer_question_stream` — a generator — suspend mid-call; only the generator's
own `yield` can do that. The embed call is pulled out into a standalone
`retrieval.embed_query(embedder, query)`; the streaming pipeline now calls it directly,
bracketed with a new `StreamStep(stage, step, phase)` event (`yield`ed before and after),
while `hybrid_search` gains an optional `query_vector` parameter so it reuses the
already-computed vector instead of embedding twice. Every other caller of `hybrid_search`
(the non-streaming `answer_question`, `features.py`'s typo-tolerance probe, `reranking.py`'s
eval harness, `eval/latency.py`, `eval/rerank_matrix.py`) is unaffected — none of them pass
`query_vector`, so the embed call still happens exactly where and when it always did.
`api/conversations.py` forwards the event over SSE as `{"type":"step","stage":"retrieving",
"step":"embed_query","phase":"start"|"end"}` — additive to the wire contract (an unrecognised
`"type"` was already a no-op in `lib/chatStream.ts`'s `dispatch`, proven by a new test).
Backend tests prove: exactly one start and one end, in order, strictly between the
"retrieving" and "reranking" stage events; never emitted at all on the chit-chat "responding"
fast path (no retrieval happens there); and the real `/chat/stream` SSE frames carry the
events in that shape end to end.

**Frontend: the steps live INSIDE the existing banner, chat only (T13.12.6).** `lib/wakeSteps.ts`
is a pure reducer (`events -> step states`, no DOM/timers) over the stream's own stage/step
events — never a timing guess, a step only becomes "current" when its own real SSE event
arrives. Six rows: *Backend starting* (container + reranker warm-up, "current" until the
first SSE event of any kind arrives — there is no dedicated backend-ready event, any event
retires it), *Waking the embeddings model* (current exactly while `embed_query`'s start/end
bracket is open), *Retrieving*, *Reranking*, *Generating*, *Checking* (one each per remaining
backend stage; the chit-chat "responding" stage is treated as *Generating* — skipping
straight there and marking embed/retrieving/reranking "done", since chit-chat never touches
them). Moving to a step marks every earlier step "done", so a skipped stage (no rerank, or
the chit-chat fast path) is never left stuck "pending" forever. `lib/useWakeSteps.ts` wraps
the reducer with React state plus a 1 s elapsed-seconds ticker for the current step's
display — the tick is `aria-hidden`, so the shared `aria-live="polite"` region only announces
an actual step change, never a per-second mutation. `components/WakeBanner.tsx` renders the
list under its existing text ONLY when a `steps` prop is passed and `state === "waking"`;
`LoginPanel`/`AdminDashboard` never pass it, so they keep the exact pre-13-J3b plain banner.

**Behaviour change: the banner now stays up until the first token, not the first event.**
`app/chat/page.tsx` used to call `wake.clearPending()` on `onConversation`/`onStage` — the
very thing 13-J3b needed to stop doing, since those fire before the slow embed even starts.
It now clears only on the first streamed token, or on `done`/`error` (the one path with no
tokens at all: an abstention with no matching chunks, or a mid-pipeline failure) — "stays
until the answer starts streaming, then clears" (DEBT-062's own wording). A fast, warm
response still never shows the banner at all: `clearPending()` still runs well under the
existing 3 s pending threshold in that case, just triggered by the first token instead of the
first event. The 13-H retry state (`waking`/`failed`) still wins over this purely-visual
layer — one banner on screen, ever — and this feature never resends/retries the question.

**Tests (T13.12.7).** `lib/wakeSteps.test.ts`: the pure state machine — initial state, backend
retiring on any event, the full walk through every step in order, a skipped reranking stage,
the chit-chat path, a stale/out-of-order event (defensive no-op), and `currentSince` tracking
the injected clock. `lib/chatStream.test.ts`: the new `"step"` event dispatches `onStep` with
`(stage, step, phase)`; an unrecognised event `"type"` is still a complete no-op (dispatches
nothing, crashes nothing). `e2e/wakeup-pipeline.spec.ts` (registered in `scripts/gate.sh`'s
`step_e2e`): against the REAL backend, held past the ~3 s threshold exactly like
`wakeup-slow.spec.ts`, asserting the step list shows "Backend starting" current and every
other step pending while nothing has arrived yet, and that the banner (with its whole step
list) disappears exactly once once the real answer streams, with the question sent exactly
once; plus its own negative control (a fast chat never shows the banner or its steps). This
file deliberately does not try to also catch the transient "embed current" render over a
real, locally-fast Ollama embed call — Playwright polls on a ~100 ms cadence, well above what
a warm local embed typically takes, so that specific mid-flight assertion would be
inherently flaky; the exhaustive ordering/transition proof for that step lives in
`lib/wakeSteps.test.ts` instead, which controls its own clock.

Out of scope for this block (13-J4, DEBT-061, same branch): warming Ollama's embedder
concurrently with the backend's own cold start, rather than serially after classification —
a separate, backend-only change that does not touch this banner.

## 13-J4: cold-start warm-up (T13.13.1–T13.13.5, DEBT-061)

**Problem.** Even with 13-J3b's real pipeline steps shown to the user, the actual cold path
stayed serial: backend container start (torch import + reranker load/warm-up, in the API
lifespan) → classify → retrieving → only THEN `retrieval.embed_query()` calls Ollama — a
SEPARATE Container App, also scaled to zero on Azure. Ollama's own cold start (image pull +
container start + loading `bge-m3`) is the slowest single thing in the whole pipeline (admin
KPI: ~19.7s avg for the "embed" stage) and it used to only START once the backend had
already finished waking up and a real question's classify/retrieve stage reached it — two
cold starts stacked one after another instead of overlapping. Azure OpenAI (the chat LLM) is
always on, so there is no equivalent "fake LLM conversation" fix here — only Ollama/`bge-m3`
is ever cold.

**Fix 1 — background warm-up at backend start (T13.13.1, `rag_app.warmup`).** The API
lifespan fires one warm-up embed call to Ollama on a background thread
(`warmup.trigger_warm_up`), **before** the (synchronous, blocking) reranker warm-up runs —
order matters: firing it first lets Ollama's own cold start run concurrently with the
*entire* remainder of the backend's own start-up, not just whatever gap happens to be left
afterwards (see the "before/after" measurement below, which proves this with the two
orderings side by side). `trigger_warm_up()` itself never blocks (it only starts a daemon
thread and returns) and never raises (any exception is caught and only logged,
`secrag_ollama_warmup_total{result="failed"}`) — readiness/`/health` never waits on it and
a failure there never fails the API start-up. A module-wide single-flight guard
(`threading.Lock` + a plain `_in_progress` flag) means at most one warm-up embed call is
ever outstanding — a second trigger while one is running is skipped
(`result="skipped"`), never queued, since the one already in flight warms Ollama up for
everyone regardless. Deliberately NOT wrapped in `rag_app.timing`'s stage/step recorder
(unlike the reranker's own `warm_up()`, which wraps purely for symmetry): a fresh OS thread
does not inherit the parent's `contextvars`, so there is never a recorder to find here even
if this ran mid-answer — a warm-up can never be averaged into a real answer's
`secrag_chat_stage_seconds`/`secrag_chat_step_seconds`.

**Fix 2 — wake ping on page load (T13.13.2, `rag_app.wake`, `GET /wake`).** `/login` and
`/chat` each send one light GET to the BFF's own `/api/wake` on page load
(`frontend/lib/wakePing.ts`, fire-and-forget, never awaited by the caller), forwarded
verbatim to the backend's unauthenticated `GET /wake`, which re-triggers the SAME
single-flight warm-up (in case Ollama went cold again after the backend itself finished
waking — e.g. the backend stayed up on a request from a different app while Ollama's own
Container App scaled back to zero on its own idle cooldown). The route always answers a
plain 204, whichever branch it took internally (warm-up started, skipped, or even an
unexpected exception from `trigger_warm_up` — caught defensively, logged, still 204): a
caller learns nothing about Ollama's/the backend's internal state beyond "accepted", so
this adds no new attack surface besides a cheap, rate-limited (`rag_app.api.deps.
rate_limit_wake`, keyed by real IP like `/csp-report`) ping. Registered in
`rag_app.features.REGISTRY` (`stack-coldstart-warmup`) and the route walker's
`PUBLIC_ALLOWLIST` (unauthenticated by design, same reasoning as `/csp-report`: it must work
from `/login` itself, before any session exists).

**Fix 3 — `embed_keep_alive`, demo mode (T13.13.3).** Ollama's own server-side `keep_alive`
default (5 minutes) would unload `bge-m3` mid-demo even with the Ollama Container App pinned
at `min-replicas 1` (`demo-mode.sh on`) if nobody asks a question for a while. Considered two
options: (A) `OLLAMA_KEEP_ALIVE` as a Container App env var, flipped on `demo-mode.sh on`/
reverted on `off` — an Azure config change on every toggle, and the thing this script's own
comment block is proud of NOT doing ("no env var/secret is ever touched by this script at
all", `test_demo_mode_never_touches_env_vars_or_secrets`); (B) a `Settings.embed_keep_alive`
field, sent as the `keep_alive` field on **every** `/api/embed` call via `OllamaEmbedder`
(not only the warm-up one — one code path, never a demo-only branch), default `"3h"`
(matching `demo-mode.sh`'s fixed `DEMO_HOURS` window). **Adopted: B.** Needs no Azure config
change at all (one backend code change, deployed once, like any other code change) and is
cost-neutral outside a demo: Azure Container Apps bills replica-seconds while a replica is
actually running, not how long Ollama happens to keep one small (1.2 GB) model resident in
memory inside an already-running container — outside a demo the Ollama Container App scales
back to `min-replicas 0` on its own idle cooldown well before 3h of inactivity elapses, so a
longer in-process `keep_alive` is simply moot by then. **Estimated extra cost: €0/h** (no
change to replica scaling, hours pinned, or app count — the existing demo-mode cost estimate,
`DEMO_MODE_HOURLY_COST_EUR` ≈ €0.17/h for the 3 apps together, is unaffected).
`demo-mode.sh on` additionally fires the wake ping once, through the same frontend BFF, right
after its existing `/api/health` check — best-effort only (`AZURE_DEMO_WAKE_TIMEOUT_SECONDS`,
default 30s): a failure/timeout there never fails or rolls back `on` (the `/api/health` check
already proved the demo is ready; worst case `bge-m3` just wakes the normal way on the first
real question).

**T13.13.4 — measured, local simulation.** Method: native `ollama serve` (bge-m3, RTX 4060),
restarted between trials to guarantee the model is genuinely unloaded ("cold"); the backend's
own process-start is simulated directly against `rag_app`'s real internals (no HTTP, no
auth) — `CrossEncoderReranker().warm_up()` for the reranker (identical call the real lifespan
makes) and `OllamaEmbedder().embed_one(...)` for the "first real question's embed call",
timed from the moment the simulated backend becomes "ready". Two orderings, 2 trials each:

| Ordering | reranker warm-up (backend "ready") | first question's `embed_query` | total |
|---|---|---|---|
| OLD (serial: reranker, then nothing happens with Ollama until a real question arrives) | ~3.85s | ~2.00s (cold) | **~5.84s avg** |
| NEW (13-J4: `warmup.trigger_warm_up()` fired first, concurrently with the reranker load) | ~3.86s | ~0.05s (already warm) | **~3.98s avg** |

Saved locally: **~1.87s (~32%)** — bounded by the smaller of the two local cold-start costs
(reranker load, ~3.85s, happens to exceed Ollama's local cold embed, ~2.0s, so the overlap
fully hides the Ollama side here). On Azure the expected saving is much larger: Ollama's own
Container App cold start (image pull + container start + model load, ~19.7s avg per the
admin KPI) is the LARGER of the two costs there, so the overlap should hide most or all of
it instead — the real Azure number comes from the user's post-promotion smoke (≥ 6 min idle,
before vs after this block), not simulated here.

**Tests (T13.13.5).** Backend: `test_warmup.py` (non-blocking — `trigger_warm_up` returns
before a deliberately blocked fake embed call completes; single-flight — a second trigger
while one is in flight is skipped, only one real call happens; failure-tolerant — a raising
fake embed call never propagates, is only logged, and the guard releases again for the next
call; the dedicated `secrag_ollama_warmup_total` metric, never the chat histograms);
`test_wake.py` (unauthenticated 204 with no body either way; per-IP rate limit + its own
`route_group="wake"` counter; defence-in-depth even if `trigger_warm_up` itself ever raised);
`test_startup.py` (the lifespan fires the warm-up BEFORE the reranker, proving the fixed
order); `test_embed_timeout.py` (every `/api/embed` call carries the configured
`keep_alive`, overridable); `test_demo_mode.py` (the wake ping fires after the health check
and is best-effort — `on` still succeeds and still keeps every app awake even when the wake
ping itself fails; `--dry-run` mentions it). Frontend: `lib/wakePing.test.ts` (exactly one
GET, fire-and-forget — a rejected promise or a synchronously-throwing fetch implementation
never escapes). Docs: `docs/APP_FLOW.md` §2.4, `docs/TRD.md` §7.
