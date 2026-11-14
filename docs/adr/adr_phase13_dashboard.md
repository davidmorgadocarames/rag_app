# ADR phase 13 — Admin dashboard + CD per-image build

- **Status:** **13-0 (CD per-image build) code-complete** on `phase-13-dashboard`, not yet
  promoted. This ADR records decisions as each block of Phase 13 lands; it will gain further
  sections (feature registry, admin API, dashboard UI, defence lab, live updates) as blocks
  13-A…13-P complete.
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
  image did not change, `migrate` already resolved and verified that same (unchanged) digest
  moments earlier in the same pipeline run, so there is nothing new to push to those two Jobs.
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
