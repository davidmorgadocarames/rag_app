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
