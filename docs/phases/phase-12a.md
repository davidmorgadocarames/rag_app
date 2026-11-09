# Phase 12a — Threat model + security baseline, part 1 (report)

- **Closing:** 2026-11-09 · **Tag:** `phase-12a` on `791b250` (the deployed commit).
- **Promoted to Azure:** 2026-11-08/09 (`main` `0d31a88` → `791b250`, fast-forward, PR #15).
- **Cost:** ≈ €0.06 posted (estimate ≈ €0.15, cap €1) — see Cost below.
- **Decisions:** [ADR phase 12](../adr/adr_phase12_security_baseline.md) ·
  **Threat model:** [`docs/security/THREAT_MODEL.md`](../security/THREAT_MODEL.md).

## Why this phase existed

Up to Phase 11 the app kept a JWT in the browser's `localStorage` (readable by any injected
script, impossible to revoke server-side), sent no security headers, exposed the interactive
API docs publicly, allowed CORS on every method and header, and keyed rate limits by the one
shared address Azure's ingress presents for every caller. Phase 12 fixes the security baseline
in two promotions: **12a** (this report — threat model, server-side sessions, CSRF, a
Backend-For-Frontend proxy, security headers and an enforcing CSP, with the backend still
public) and **12b** (roles + admin MFA, lockout, password policy and reset, real e-mail, and
the backend's ingress turned internal-only).

## What was done

| Block | What | Result |
|---|---|---|
| A | Threat model (12.0) | `THREAT_MODEL.md`: STRIDE per trust boundary plus a 37-entry catalogue (OWASP Web / API / LLM) with difficulty, attack level, owning phase and the test that proves each mitigation; a `threat-catalogue` gate step fails on a malformed row or a "mitigated" entry without a real test. Azure configuration checked against it (read-only). |
| B | Server-side sessions | Migration **0006** (`sessions` table + `users.role`, expand-only); opaque session IDs stored hashed in Postgres, `__Host-` `HttpOnly` cookie, 30 min idle / 12 h absolute, server-side logout, all sessions revoked on account erasure; legacy Bearer kept for the rollback path; new `tests/security/` suite + `security-suite` gate/CI step. |
| C | CSRF + BFF + signed client IP | Signed double-submit CSRF token (`CSRF_SECRET`) + `Origin` check; Next.js BFF route handlers proxying an explicit allow-list of backend routes (anything else is a 404), upstream timeout ≥ 240 s so cold starts still fit; the BFF signs the real client address (`CLIENT_IP_HMAC_KEYS`) and the backend keys rate limits by it. |
| D | Frontend on cookies | The browser never sees a token again (`access_token` stripped by the BFF; `localStorage` session code deleted); UX-only route guard; `/login` split into an SSR shell + client form; Mailpit for local e-mail (the verification link no longer appears in any response); Playwright end-to-end test of register → login → chat → logout through the BFF. |
| E | Headers, CSP, surface reduction | HSTS (no preload), `nosniff`, `Referrer-Policy`, `Permissions-Policy` on every frontend response; per-request nonce CSP with `strict-dynamic`, **enforcing** after a Playwright walk of every page showed zero violations; a rate-limited, size-capped `/csp-report` endpoint; backend `/docs`, `/redoc`, `/openapi.json` only with `ENV=dev`; CORS narrowed to what the pre-BFF frontend needs; new `security-headers` and `zap-baseline` (OWASP ZAP, digest-pinned) gate steps. |
| E2 | Review fixes | CSP-report log injection closed (directive field capped and sanitised); an empty or partial ZAP report now fails the step instead of passing silently. |
| E3/E4 | Dependency advisories | 68 advisories published during a three-week pause blocked the first merge push; every affected pin bumped (Python + `next`); an image library that was never shipped was dropped again; the backend base image is now pinned by digest like the others. |
| P | Promotion | Two new backend secrets + frontend settings added before the merge and verified in the running replicas (by length only), merge, CD, migration 0006, Azure smoke. |

## Deviations from the plan (accepted by the user)

- **Frontend → backend over the backend's public HTTPS address (DEC-017).** The plan had the
  BFF call the backend by its in-environment app name. With the backend still on external
  ingress (and plain HTTP disallowed on it), that form had no precedent in this environment,
  so 12a uses the backend's public `https://` address. Harmless while the backend is public
  anyway; 12b switches to the internal address when the ingress goes internal.
- **New secrets added in place, not by recreating the app (DEC-016).** An earlier Azure
  environment had ignored new environment variables until the app was recreated; in the
  current standard environment `secret set` + `update` produced a new revision and the values
  were verified (length only) in the new replicas, so no downtime-causing recreate was needed.
- **Cold-start signed-IP staleness (DEC-021).** The smoke's fully-cold chat request logged one
  signed-IP fallback: the BFF signs at request time, but a cold backend verifies only after
  waking (~69 s), past the 30 s signature window. Only the rate-limit key of the first
  request after a cold start is affected. Fix planned as the first task of 12b (a longer
  window bound to method + path, plus a privacy-safe verified/fallback counter).
- **A dependency fix block before the merge (DEC-018/019/020).** The promotion paused from
  2026-10-21 to 2026-11-07; on resuming, the pre-push `--full` gate found 68 new advisories
  and rejected the push (nothing reached `main`). Most turned out to come from stale packages
  in the long-lived local audit environment rather than from any shipped image — tracked as a
  follow-up to audit the resolved requirements instead.
- **Two-phase blocks.** When the development machine was busy, blocks ran in two phases:
  code and targeted tests at low priority first (local commits only), then the gate, push, CI
  and review once the machine was free.

## Gate evidence

- `gate.sh --full` **PASS 545 s** on `791b250` (pre-push hook at the `main` push; latency
  unchanged against the baseline; `restart-check` 88 s); `secrag/gate-full` published.
- Earlier full runs on the branch: 393 s (`2f37588`), 435 s (`b1ba205`), 551 s (`7e28699`,
  first `--full` with e2e + ZAP + CSP enforcing), 507 s (`010f0fb`).
- CI **7/7** required checks on `791b250` (run `42531039177`).
- Diff `0d31a88..791b250`: 14 commits, 95 files, +5925 / −385.

## Azure promotion evidence

- CD run `42531591988` green; migrate Job applied **0006**.
- Deployed digests: backend `rag_app-backend@sha256:0a92f59d40cdf165d419bdf11f1bf4227e5df6e6192653f886b6f6d3542c3c14`,
  frontend `rag_app-frontend@sha256:452747f7d69344ba63f2e57577d6c0d0f4d20245d13f983bc3ce5b12129915f8`
  (Ollama and jobs images rebuilt by the same run).
- Rollback (pre-12a) digests: backend `sha256:83027a258ea5355591925212981b029cce7d79927b0656580d6f5f0cc4aa5231`,
  frontend `sha256:dc0b60a77c6160d3ddd66a86942d5e9e7d0427ee722ff5656996bae78b6efef4`, Ollama
  `sha256:00333ab77cc1f1a7ddec2a540a037127302344de7ef17894e16b737fe5a5f3fd`, jobs
  `sha256:abd004382467d1b262ec30504d735b73e161f4775c646f006901095f8fa0c9f5`. Images roll back;
  the schema does not (0006 is expand-only, the old backend ignores it) — see the ADR.
- **Smoke PASS:** HSTS / `nosniff` / Referrer / Permissions headers and an enforcing nonce CSP
  on `/` and `/login`; `/docs`, `/redoc`, `/openapi.json`, `/metrics` 404 on both apps;
  register and login through the BFF set both `__Host-` cookies and return no `access_token`;
  a state-changing request without the CSRF token → 403; a foreign `Origin` → 403; the
  backend without a cookie → 401; **a fully cold chat question through the BFF: HTTP 200,
  first byte 69 s, total 122 s** (under the ~240 s ingress limit) with citations; no smoke
  e-mail or test address in the logs; smoke account erased; every app back to 0 replicas.

## Cost

Phase estimate ≈ €0.15 (cap €1). Measured (Azure Cost Management, `ActualCost`, read-only,
whole subscription, queried 2026-11-09): the pre-merge configuration day 2026-10-21
**€0.057** (new revisions while the October Container Apps free grant was already used up)
and the merge + smoke 2026-11-08/09 **≈ €0.001** so far (Azure OpenAI; Container Apps €0
inside November's free grant; 2026-11-09 not fully posted yet). **12a ≈ €0.06.**

**Container Apps billing, settled.** October was the first full month to show it: Container
Apps **€1.05** of the month's **€1.08** — €0 until 2026-10-18, then **€0.81** on 2026-10-19
(an interview demo left on ~6 h 50 min, i.e. ≥ €0.12/h for the three apps) and ~€0.01/day
afterwards for the scheduled purge and backup Jobs. So the standard environment's monthly
free grant covers the first part of each month's usage and is then billed normally; the
demo-mode estimate of €0.15–0.17/h stays as the upper bound. November is back at €0 so far.
Subscription total since 2026-09-01: **€7.18 of the €40** budget.

## Changed docs

[THREAT_MODEL](../security/THREAT_MODEL.md) (new), [ADR phase 12](../adr/adr_phase12_security_baseline.md)
(new), [TRD §8.1](../TRD.md) (BFF, sessions, CSRF, signed IP, headers, CSP),
[APP_FLOW](../APP_FLOW.md) (cookie / BFF / CSRF flow), [BACKEND_SCHEMA](../BACKEND_SCHEMA.md)
(migration 0006), [README](../../README.md) (third required secret `CSRF_SECRET`,
`CLIENT_IP_HMAC_KEYS`, `/docs` dev-only), [Definition of Done](../DEFINITION_OF_DONE.md) (new gate
steps), [ADR phase 11](../adr/adr_phase11_stability.md) (Container Apps billing settled).

## Known follow-ups

- **DEBT-034** — until 12b makes the backend internal, a request that reaches the public
  backend directly is rate-limited on the shared Azure address (targeted per-account login
  throttling possible; not worse than before 12a).
- **DEBT-038** — signed client IP stale on a cold start (above); first task of 12b.
- **DEBT-039** — Dependabot: development-only `postcss`, `postcss-selector-parser` and
  `minimatch` bumps early in 12b; the `postcss` copy bundled inside Next.js stays a dated,
  build-time-only exception until the Next.js major upgrade (DEBT-003, Phase 15).
- **DEBT-037** — the dependency audit should check the resolved requirements, not a
  long-lived local environment (Phase 15, with the hashed lock file).
- **DEBT-036** — the Azure OpenAI model version in use retires on 2027-04-14; a deliberate,
  evaluated migration is planned in Phase 14.
