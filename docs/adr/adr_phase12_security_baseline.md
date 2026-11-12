# ADR phase 12 — Security baseline: sessions, BFF, CSRF, headers/CSP

- **Status:** **12a accepted and promoted** 2026-11-08/09 (`main` `791b250`, tag `phase-12a`,
  report [`docs/phases/phase-12a.md`](../phases/phase-12a.md)); this ADR records the
  **12a part** (12.0 threat model, 12.1 sessions/BFF/CSRF, 12.2 security headers + surface
  reduction) below under "Decisions", and the **12b part** (RBAC, MFA, email verification,
  lockout, Bearer/CORS removal, surface reduction) in its own "## 12b" section further down.
  **12b is code-complete on `phase-12b-identity-email` (draft PR #19) but not yet promoted** —
  promotion itself (T12.6b: internal ingress, Azure smoke, tag) is still pending.
- **Related:** [`docs/security/THREAT_MODEL.md`](../security/THREAT_MODEL.md) (STRIDE +
  vulnerability catalogue this ADR's decisions close entries against), [TRD §8.1](../TRD.md)
  (implementation summary), [ADR phase 11](adr_phase11_stability.md) (migration discipline
  0005 → **0006**, the first migration this phase adds; 12b adds **0007**, see "## 12b" below).
- **Findings closed:** 12a closed F1 (JWT in `localStorage`, no server-side logout), F2 (no
  security headers), F4 (rate limits keyed by the shared Azure connection address), F7
  (`/docs` public), F8 (CORS `*` methods/headers). **12b closes F3, F5, F6, F10, F11** — see
  THREAT_MODEL §"Findings verified in the code" and the "## 12b" section below.

## Context

Phase 11 closed data persistence and latency; the app had no session model (a client-side
JWT in `localStorage`, never revocable server-side), no CSRF protection, no security headers,
a fully public interactive API surface (`/docs`, `/redoc`, `/openapi.json`), and CORS wide
open on methods/headers. Phase 12 is split into two promotions (12a, this ADR; 12b, backend
ingress goes internal + RBAC/MFA/real email verification) because 12b's internal ingress
needs its own health-check/smoke rework (DEC-005) that is independent of the session/CSRF/
header work here.

## Decisions

### P12-D1 — BFF (Backend-For-Frontend)

Next.js route handlers (`frontend/app/api/[...path]/route.ts`) proxy an **explicit
allow-list** of `(method, path-pattern)` pairs (`lib/bffRoutes.ts`) to the backend — anything
not listed is a 404 by construction, never a catch-all forward. This buys: a first-party
cookie (no CORS needed for the new frontend at all), the backend can go internal-only in 12b
without the frontend needing a public path to it, and a single choke point that can never
accidentally expose `/metrics`/`/docs`. The upstream timeout is **≥ 240 s**
(`MIN_TIMEOUT_MS`, DEC-006) so the Azure ingress's own ~240 s cutoff — not a shorter BFF
timeout — stays the real limit on a cold/slow answer (11b's cold-start fix must not be
silently shortened by the new hop); the response body streams through unbuffered once headers
arrive, so SSE survives the hop token-by-token. `access_token` is stripped from
`/auth/login`/`/auth/register` responses before the browser ever sees them.

### P12-D2 — CSRF: signed double-submit + `SameSite=Lax` + `Origin` check

`rag_app.csrf`: `csrf_token = HMAC-SHA256(CSRF_SECRET, raw_session_id)`, issued in a
**readable** `__Host-secrag_csrf` cookie (unlike the session cookie, not `HttpOnly` — page
script must be able to read it to echo it back). `require_csrf` on every
cookie-authenticated state-changing route; a request with **no session cookie at all** (the
legacy Bearer-only rollback path, TF10) is exempt, since a bearer token is never sent
automatically by the browser and so cannot be forged cross-site. `require_origin` (lighter:
rejects a present-but-wrong `Origin`, allows a missing one) covers register/login, where no
session exists yet to bind a token to.

- **DEC-010 = A — `CSRF_SECRET`:** a new, independent secret (≥ 32 chars, fail-fast with
  `ENV=prod`), rather than deriving it from `JWT_SECRET` (option B — rejected because
  `JWT_SECRET` is meant to go away once Bearer is removed in 12b) or reusing
  `CLIENT_IP_HMAC_KEYS` (option C — rejected, mixes two unrelated purposes).
- **DA-12aC login-without-`Origin` trade-off:** `require_origin`'s "missing `Origin` is
  allowed" half is a deliberate compatibility decision, not an oversight — some real browser
  requests (and every `curl`/legacy client) omit `Origin` on a same-origin request; the
  control that actually stops a cross-site forger is that **a forger cannot set a matching
  `Origin` at all**, so allowing "no `Origin` header" through costs nothing against that
  threat model while a strict "`Origin` required" rule would reject real traffic. Recorded
  here because a future tightening (e.g. once every supported browser is confirmed to always
  send `Origin` on these routes) should treat this as a conscious loosening to reconsider, not
  a bug to "fix" silently.

### P12-D3 — Session lifetimes

30 min idle / 12 h absolute for ordinary users; 15 min / 8 h for admins (12b, once roles
exist). `last_seen_at` is throttled (≥ 60 s between writes) to avoid a write per request.

- **DEBT-032 (cookie has no `Max-Age`/`Expires`):** `__Host-secrag_session` is set without an
  explicit lifetime, making it a **browser-session cookie** by the spec's own definition (gone
  when the browser process closes) — this is intentional, not a bug: the server's own
  idle/absolute expiry (above) is the real limit regardless of what the browser does with the
  cookie's lifetime claim, and omitting `Max-Age` avoids asserting a client-controlled
  lifetime that could outlive the server's. The DA-12aB-2 finding that raised this was
  informational (no fix required); this paragraph is that documentation.

### P12-D1 (continued) / T5-V7 — Signed client IP

Once `CLIENT_IP_HMAC_KEYS` is set, the BFF signs the real client address (derived from
`X-Forwarded-For`, `rag_app.client_ip` / `frontend/lib/clientIp.ts`) and the backend verifies
it before keying rate limits by it; a missing/forged/stale signature falls back to the raw
connection address and emits a security event.

- **DEC-011 = A:** the fallback event only fires once a key is actually configured (an
  unconfigured feature emits nothing — there is nothing suspicious about a control that was
  never turned on) **and** `CLIENT_IP_HMAC_KEYS` is required with `ENV=prod` (fail-fast), so
  "no keys configured" can only ever happen in dev, never silently in production.
- **Accepted gap (DA-12aC-2, DEBT-034, until 12b):** this only protects callers that actually
  go through the BFF. A request that reaches the still-public 12a backend directly falls back
  to the shared Azure connection address, collapsing `login:<email>:<ip>` to effectively
  `login:<email>` for that path — a targeted per-account login-throttling angle, but **not
  worse** than the pre-12a pure-IP keying (where one actor on the shared address could already
  throttle everyone). Closes fully once the backend's ingress goes internal (12b).

### Security headers + CSP (12.2) + surface reduction

- **Frontend** (`frontend/middleware.ts`, `lib/csp.ts`, `lib/securityHeaders.mjs`,
  `next.config.mjs`): HSTS (**no `preload`**, E7 — the deployed hostname is a shared
  `*.azurecontainerapps.io` domain this project does not own outright, so permanently
  asserting HTTPS for every sibling subdomain under that suffix is not this project's call to
  make), `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`, `poweredByHeader: false` on every
  response; a per-request nonce CSP (`script-src 'self' 'nonce-…' 'strict-dynamic'`,
  `style-src 'self' 'unsafe-inline'` — Next's own CSS-in-JS/`next/font` machinery injects
  unnoncable `<style>` tags, the plan's documented trade-off — `frame-ancestors 'none'`) on
  every HTML route under a matcher that excludes only `_next/static`, `_next/image`,
  `favicon.ico` and `/api/...`.
  - **`CSP_MODE` single switch (`lib/csp.ts`):** `"report-only"` until a Playwright page walk
    proves **zero** `script-src` violations against the real built app in a real browser,
    then flipped to `"enforce"` — T12.2.3's literal Done-when is "zero violations **with CSP
    enforcing**", not "zero violations in report-only mode". **`CSP_MODE` is `"enforce"`
    since 2026-10-17:** the walk showed zero violations both before and after the flip, and
    the Azure smoke confirmed the enforcing header (nonce + `strict-dynamic` +
    `frame-ancestors 'none'`) on `/` and `/login`.
- **Backend** (`rag_app.api.app`): the same handful of headers
  (`X-Content-Type-Options`, `Referrer-Policy`, `Content-Security-Policy: frame-ancestors
  'none'`, `Cache-Control: no-store`) on every API response via one ASGI middleware;
  `/docs`/`/redoc`/`/openapi.json` registered only with `ENV=dev` (`None` otherwise — a true
  404, not an auth-gated route); CORS narrowed from `allow_methods=["*"]`/
  `allow_headers=["*"]` to exactly what the **old**, pre-BFF frontend needs for the 12a
  rollback path (`GET`/`POST`/`PATCH`/`DELETE`; `Authorization`/`Content-Type`; single
  origin; `allow_credentials=False`, since that frontend sends a Bearer token, never a
  cookie) — removed entirely once 12b makes the backend internal-only.
- **CSP reporting** (`rag_app.csp_report`, E5): a new, **unauthenticated**, explicitly
  **CSRF-exempt** backend route (no session exists to protect; a cross-site forger cannot
  "forge" a violation report, which is itself the information this route exists to collect),
  rate-limited by the real signed client IP, body-size capped (8 KiB), accepting both the
  legacy `application/csp-report` shape (`report-uri`, read by Firefox/Safari) and the
  Reporting API's `application/reports+json` shape (`report-to` + a `Reporting-Endpoints`
  response header, read by Chromium) — a browser may batch several reports per POST under the
  newer shape. Logs only the **violated directive** and the **blocked origin** (scheme + host
  only — never a path or query string, matching the T11.2.15/R6-5 log-hygiene pattern already
  used for the e-mail verification link).
- **DEC-013 (related, found alongside this work in 12a-D):** `send_verification_email` swallows
  a transient SMTP `OSError` rather than 500ing register/resend — kept (option A), with
  sent/failed visibility planned via 12b's T12.4.7 counters rather than a louder failure now.

### Rename: `NEXT_PUBLIC_API_URL` name collision (DEBT-035 part 2)

The backend had its own, unrelated setting also named `next_public_api_url` (only used to
build the e-mail verification link in `emailer.py`) — never read from the frontend's
build-time env var or the GitHub Actions repo variable of the same name (confirmed: grepped
the whole repo), but the shared name risked confusing whoever removes that GitHub variable at
12a-P. Renamed to `EMAIL_LINK_BASE_URL` / `Settings.email_link_base_url`; no compatibility
break (nothing in `.env`, compose or CD ever set the old name for the backend).

**12b-B follow-up:** renamed again, to `PUBLIC_APP_URL` / `Settings.public_app_url`, once
T12.4.5 built the frontend's own `/verify`/`/reset` pages and the link had to point there
instead of at the backend directly (the field's whole reason to exist). Same
"no compatibility break" note applies.

## Promotion (12a) — decisions taken at deploy time

- **DEC-016 = A — new secrets in place.** `CSRF_SECRET` and `CLIENT_IP_HMAC_KEYS` were added
  with `az containerapp secret set` (values typed by the operator, never in argv or history)
  plus `update --set-env-vars …=secretref:…`, which created a new revision; verified in the
  running replicas by **length only**. The recreate-the-app fallback (needed once on an older
  environment) was not required.
- **DEC-017 = A — deviation from P12-D1.** The frontend's `BACKEND_URL` is the backend's
  **public `https://` address** in 12a, not the in-environment app name: the backend still has
  external ingress with plain HTTP disallowed, and nothing in the environment proved the bare
  app-name form works on it. No exposure added (the backend is public in 12a anyway, DEBT-034);
  12b switches to the internal address together with the internal ingress.
- **DEC-015 = A.** The backend's stale `NEXT_PUBLIC_API_URL` variable (read only by the old
  e-mail-link setting, renamed above) was removed in the same update; the GitHub repository
  variable of the same name was deleted after the smoke.
- **DEC-021 = A — known gap found by the smoke.** A fully cold request through the BFF arrives
  at the backend ~69 s after the BFF signed the client address, past the 30 s window, so that
  one request falls back to the connection address (and logs a security event). Accepted for
  12a; 12b widens the window (bound to method + path) and adds a privacy-safe verified/fallback
  counter (DEBT-038).

## Rollback

- **Rollback digests (pre-12a):** backend
  `rag_app-backend@sha256:83027a258ea5355591925212981b029cce7d79927b0656580d6f5f0cc4aa5231`,
  frontend `rag_app-frontend@sha256:dc0b60a77c6160d3ddd66a86942d5e9e7d0427ee722ff5656996bae78b6efef4`,
  Ollama `rag_app-ollama@sha256:00333ab77cc1f1a7ddec2a540a037127302344de7ef17894e16b737fe5a5f3fd`,
  jobs `rag_app-jobs@sha256:abd004382467d1b262ec30504d735b73e161f4775c646f006901095f8fa0c9f5`.
  Deployed 12a digests: backend `…0a92f59d40cdf165d419bdf11f1bf4227e5df6e6192653f886b6f6d3542c3c14`,
  frontend `…452747f7d69344ba63f2e57577d6c0d0f4d20245d13f983bc3ce5b12129915f8`.
- **Images roll back; the schema does not** (same principle as ADR phase 11). Migration
  `0006` (`sessions` + `users.role`) is expand-only; the pre-12a backend image never reads
  either new table/column, so a rollback to the previous backend digest still works against
  the post-0006 schema.
- The **old, pre-BFF frontend** still authenticates against the **new** backend during 12a:
  `auth.py` still returns `TokenResponse(access_token=...)` on register/login (Bearer path
  unaffected) and `CORSMiddleware` still allows the frontend's origin (narrowed, not
  removed) — so a rollback to the old frontend digest needs no backend change. `BACKEND_URL`/
  `CLIENT_IP_HMAC_KEYS`/`CSRF_SECRET` can stay set on Azure after a rollback; the old code
  simply ignores the ones it doesn't use. **This paragraph is 12a-era history only, no longer
  true from 12b onward** (DA-12bD-2): 12b removed `HTTPBearer`, the Bearer fallback in
  `get_current_user`, the JWT in `TokenResponse` and `CORSMiddleware` entirely (see "## 12b" →
  "Bearer/JWT/CORS removed entirely" below). Rolling back only the frontend digest to this
  old pre-BFF one while keeping a 12b+ backend gets a 401 with no CORS headers at all, not the
  "no backend change" promise above — from 12b onward a rollback must always move backend and
  frontend together, matching T12.6b.2's plan and the general "images roll back together"
  principle (ADR phase 11), just now written down as a *reason*, not only a plan.
- A rollback to a **pre-12a-E** frontend/backend digest (before this ADR's headers/CSP/CORS
  narrowing/docs-gating) is also safe in the other direction: none of 12a-E's changes are
  stored state — headers, CSP mode and the docs-gating condition are all read fresh from
  config/code on every request, nothing persists them.

## Costs (12a)

Estimate ≈ €0.15, cap €1. Measured (Azure Cost Management, read-only): **≈ €0.06** — €0.057
on 2026-10-21 (pre-merge configuration revisions) and ≈ €0.001 on 2026-11-08/09 (merge, CD,
smoke; Container Apps inside November's free grant). Detail in the
[phase report](../phases/phase-12a.md#cost).

## 12b — RBAC, MFA, email verification, lockout, surface reduction

12b is split across four blocks on the same branch (12b-A: RBAC/MFA/admin bootstrap; 12b-B:
lockout/password policy/register-verify contract; 12b-C: password reset/email abuse limits;
12b-D: Bearer/CORS removal, route walker, health-via-BFF, exposed-files check). Migration
**0007** (`user_mfa`, `login_lockouts`) is the one this part of the ADR adds, on top of 12a's
0006 (`sessions`, `users.role`).

### Context

12a closed the session/CSRF/header gaps but left F3 (account-enumeration timing), F5 (no
lockout), F6 (no roles), F10 (no password policy) and F11 (no real email verification) open,
and kept the legacy Bearer path + narrowed (not removed) CORS alive as the 12a rollback path
(DEBT-034: the backend still has public ingress, so a caller that skips the BFF still
collapses rate-limit keys to the shared Azure address). 12b closes all five findings, removes
Bearer/CORS entirely, and prepares the backend for the internal-only ingress that T12.6b.5
will flip — DEBT-034 itself only closes there, not in this ADR's own scope.

### Decisions

#### P12-D4 — RBAC: role → permission map, never a role-string comparison (T12.3.2b)

**Decision:** `rag_app.authz.require_permission`/`require_permission_and_mfa` resolve every
protected route against a `ROLE_PERMISSIONS: dict[str, frozenset[str]]` map, not a role-string
`if user.role == "admin"` check; `require_role` is kept only as a thin alias for role-level
(not data-level) placeholder checks, new routes should prefer `require_permission` directly.
**Fails closed on a permission unknown to the map** — denies every role, including admin,
rather than silently granting it (fail-open) or silently denying only some roles (hard to
notice) — proved by `backend/tests/security/test_authz.py::
test_an_unknown_permission_denies_every_role`. `DECLARED_PERMISSION_ROUTES: dict[tuple[str,
str], str]` records every route's `(method, path) -> permission`, read later by the T12.5.2
route walker (P12-D11 below); `PROTECTED_ROUTES` is the narrower, admin-grade subset the
T12.3.6 client-tampering suite (role × route matrix, generated from the live registry, not a
hand-maintained list) can safely probe bare. `role` is never accepted from the client and
never cached anywhere the client can read back — the backend re-reads `user.role` from the
database on every request (`rag_app.sessions.authenticate`); the **only writer of `role` is
the admin bootstrap CLI** (`rag_app.admin`, interactive password via `getpass`, no API route
ever sets it) — no PATCH-the-role endpoint exists to tamper with in the first place. Admin
routes additionally require a fresh MFA step-up on the current session (P12-D5).
**Alternatives considered:** (a) a per-route role string (`if user.role == "admin"`) —
rejected, the exact anti-pattern this map exists to avoid (a typo silently fails open, no
central place to audit who can do what); (b) an API route to change a user's role — rejected,
every write path is attack surface the CLI-only design removes entirely; a wrong role can
only ever be fixed by an operator with shell access. **Consequences:** adding a future
department role (Phase 21: HR, engineering, software...) is one `ROLE_PERMISSIONS` entry plus
a migration, never a route-level code change (the map, not the routes, grows) — the explicit
design goal recorded in `rag_app/authz.py`'s own module docstring; every protected route's
auth/permission/rate-limit triple is enforced structurally by the walker (P12-D11), not by
code review alone.

#### P12-D5 — MFA: encrypted TOTP secret, hashed recovery codes, step-up rotates the session (T12.3.4/T12.3.5, migration 0007)

**Decision:** `rag_app.mfa`/`rag_app.api.admin` (`pyotp`): the TOTP secret is encrypted at
rest with the master key (`rag_app.crypto.wrap_key`, the same primitive other at-rest secrets
in this project already use) in `user_mfa.secret_encrypted`, never logged. Recovery codes are
generated once, shown once, and stored only as `hash_recovery_code` (`rag_app.security.
hash_token`, the project's existing token-hashing primitive) — the plaintext code never
touches the database. Claiming a recovery code (`claim_recovery_code`) is a single atomic
`UPDATE … WHERE used_at IS NULL` (X10, P12-D9 below). A successful MFA step-up **rotates the
session id** (`session_service.rotate`, `rag_app.api.admin.mfa_step_up`) and reissues the
session/CSRF cookies, so a session that was only ever authenticated (not stepped-up) cannot
be upgraded to admin-grade just by a later cookie leak reusing the same id. Client-side: the
`otpauth://` provisioning URI is rendered as an inline SVG QR code
(`frontend/components/QrCode.tsx` + `frontend/lib/qrMatrix.ts`) — **DEC-024 = B**, package
`qrcode-generator@2.0.4` (pinned exact, zero runtime dependencies, MIT), chosen over a
canvas-based wrapper (e.g. `qrcode.react`, option A — rejected, pulls in `canvas`/`pngjs` for
a feature that only needs the boolean matrix) because a plain `<svg>`/`<rect>` tree needs no
`dangerouslySetInnerHTML` and no exception to the nonce CSP (`security-headers` stays PASS
with zero violations). Manual secret/URI entry is kept as the fallback for any TOTP app.
**Alternatives considered:** storing the TOTP secret in plaintext (rejected — a DB dump would
hand out a working second factor for every admin account) or hashing it like a password
(rejected — the server must read the secret back to compute the running code, hashing is
one-way and would make TOTP unverifiable); storing recovery codes in plaintext for display
again later (rejected — undermines "single-use secret", a one-time reveal + hash is the
standard pattern this project follows). **Consequences:** a master-key rotation or loss is
now also an MFA-secret availability question (same blast radius as every other master-key-
wrapped secret in this project, not a new category of risk); the recovery-code single-use
race (REC-04) and the step-up session rotation both needed their own concurrency/structural
tests (`backend/tests/security/test_mfa.py`) because a check-then-use shape here would be a
silent authentication bypass, not just a cosmetic bug.

#### P12-D6 — Authentication hardening: no account-enumeration via timing or response shape (T12.4.1, T12.4.3, T12.4.4, DA-12bB-1)

**Decision:** Login is constant-time regardless of whether the account exists
(`rag_app.security.dummy_password_hash`, verified by `backend/tests/test_security.py::
test_login_timing_does_not_distinguish_a_real_account_from_a_missing_one`). `/auth/register`
pays the **same** cost on every call — the argon2 hash runs unconditionally and the handler
always attempts the same three inserts (`User`, `UserKey`, `EmailVerificationToken`); an
address that already has an account differs only in that the unique index on `users.email`
rejects the insert, discarded by a rollback rather than detected in advance by a cheaper
existence `SELECT` (`backend/tests/security/test_register_timing.py::
test_register_timing_does_not_distinguish_an_existing_account_from_a_new_one`, the DA-12bB-1
fix — the two outcomes are literally the same code path up to the point of divergence, not
just two paths tuned to cost the same). `register`/`resend-verification`/`forgot-password`
all return a **generic 202** whatever the outcome — never a 409 or a "no such account" 404 —
and register issues no session: the account only becomes usable once the email verification
link is followed (`require_email_verification` defaults `True` with `ENV=prod`, fail-fast if
unset, explicit `False` survives the default — `backend/tests/test_startup.py`). Password
policy (`rag_app.password_policy`, T12.4.3): minimum length, rejects the email local-part as
the password, and checks against the NCSC/SecLists top-100k breached-password list
(`backend/src/rag_app/data/password_denylist_ncsc_top100k.txt.gz`, sha256-pinned by
`backend/tests/test_password_policy.py`) rather than an entropy/zxcvbn-style score.
**Alternatives considered:** an upfront existence check before deciding register's response
(rejected — DA-12bB-1's whole point, the fastest way to reintroduce the timing side-channel
F3 closes); a distinct 409 for "already registered" (rejected — the generic 202 is what
actually removes the enumeration signal, a differently-shaped response would undo it even
with matched timing); entropy-based password scoring (rejected — NCSC's own published
guidance for public-sector-grade services prefers a breach-list check, and it is simpler to
reason about than a scoring heuristic). **Consequences:** every "does this account exist"
question an attacker could ask now gets the same answer in the same time, by construction,
not by a review checklist; the trade-off is a less informative UI (register never tells a
user that address is already theirs) — acceptable for this threat model.

#### P12-D7 — Lockout keyed by HMAC(email), exponential back-off (T12.4.2)

**Decision:** `rag_app.lockout` keys the failed-login counter by `HMAC(LOCKOUT_HMAC_SECRET,
email)`, not by `user_id` or the raw email — the row must exist and be queryable **before**
the backend knows whether the account is real (a login attempt against a non-existent email
still needs a counter to rate-limit guesses against that address), and the raw email must
never appear in a log or in the table itself. `record_failed_login` is a single atomic
`INSERT … ON CONFLICT … DO UPDATE` (X10, P12-D9); exceeding the threshold locks the account
with an exponentially growing back-off
(`backend/tests/security/test_lockout.py::
test_the_next_failure_past_the_threshold_locks_and_back_off_grows`). A successful
`/auth/reset` clears the lockout and revokes every session of the account
(`test_reset_password_success_revokes_sessions_and_clears_the_lockout`); account erasure
deletes the `login_lockouts` row for that `email_hmac` (DA-12bB-2). **Accepted gap (DEBT-034,
open until the backend's ingress goes internal, T12.6b.5):** this only protects callers that
go through the BFF and get their client IP HMAC-signed; a request straight to the still-
public backend collapses the real-IP axis of the lockout key, same caveat 12a already
recorded for login rate limiting (P12-D1 continued / T5-V7 above) — not worse than the pre-
12a keying, and the email-HMAC axis (which does not depend on the client IP at all) still
applies either way. **Alternatives considered:** keying by `user_id` (rejected — would need
an existence lookup before the counter can be touched, reopening the exact timing/enumeration
signal P12-D6 just closed); storing the raw email as the key (rejected — same reasoning as
the CSP-report/log-hygiene convention elsewhere in this project, an identifier an attacker
supplies should never be directly queryable/loggable in plaintext). **Consequences:**
`LOCKOUT_HMAC_SECRET` joins `CSRF_SECRET`/`CLIENT_IP_HMAC_KEYS` as a devtool/test-harness
dependency that is easy to forget (two real bugs found and fixed this way during 12b-B/12b-D,
`erasure_scale.py` twice) — worth a generic "every devtool that talks to a live API needs
every required secret" checklist item for future phases, not a code fix here.

#### P12-D8 — Email verification/reset: frontend pages POST the token, four-bucket abuse limiter, X10 everywhere (T12.4.5–10, T12.4.6 DA re-check)

**Decision:** The verification/reset links point at the frontend's own `/verify`/`/reset`
pages (`PUBLIC_APP_URL`, renamed from `EMAIL_LINK_BASE_URL` once these pages existed to need
it — no compatibility break, nothing in `.env`/compose/CD ever set the old name for a
production value), which read the token from the query string and **POST** it to the backend
rather than the backend handling a bare `GET` on the link itself (keeps the token out of
server/proxy access logs and browser history as a *followed* request, and lets the page show
a clear success/failure state). Consuming either token
(`_consume_password_reset_token`/`_consume_email_verification_token`) is, like the recovery
code and the lockout counter, a single atomic conditional `UPDATE … WHERE used_at IS NULL AND
expires_at > now()` — never SELECT-then-UPDATE (REC-06, same X10 template as REC-04/05, see
P12-D9). `rag_app.email_abuse.EmailAbuseLimiter` rate-limits `register`/`resend-verification`/
`forgot-password` with **four** independent token buckets checked in order
`ip → recipient_ip → recipient (backstop) → global`, combined with Python's short-circuit
`and` so a denial at an earlier bucket never also spends a token it didn't need to in a later
one. The `(recipient, IP)` bucket — **DEC-026 = A**, added in the 12b-C fix block — caps one
source IP's share of a recipient's traffic below the pure-recipient backstop's own
capacity/refill rate, closing the gap where a single IP could otherwise drain the backstop
and deny the real account owner (`backend/tests/test_email_abuse.py::
test_single_attacker_ip_trickling_never_starves_the_victim_from_another_ip`); the accepted
residual is that **roughly 7** distinct, non-colluding attacker IPs trickling slowly enough
can still exhaust the recipient backstop between them
(`test_many_ip_flood_still_hits_the_recipient_backstop`) — the same mitigation-layering trade-
off DEBT-034 already accepts for login lockout, not a new risk category. The recipient bucket
key is `HMAC(random-32-byte-per-process-salt, email)`, never the raw address
(`test_recipient_bucket_keys_never_contain_the_raw_email`). **Alternatives considered:** a
bare `GET` verify/reset link that the backend consumes directly (rejected — leaks the token
into access logs/history on the very request meant to burn it); a single global or per-
recipient-only bucket without the `(recipient, IP)` layer (rejected, DEC-026 — DA-12bC-1 found
a single trickling IP could drain the per-recipient backstop alone). **Consequences:** the
limiter is **in-memory, single replica** (the project runs one backend replica by budget,
RISK-03/DEBT-010) — a future multi-replica scale-out needs a shared store (Redis or
equivalent) for these buckets, tracked as **DEBT-010**'s existing scope, not a new debt row.

#### P12-D9 — X10: every single-use token/counter consume is one atomic statement, never SELECT-then-UPDATE (REC-04..07)

**Decision:** Project-wide rule (X10, 2026-11-09) applied consistently across 12b: the MFA
recovery code (REC-04), the lockout counter (REC-05, `INSERT … ON CONFLICT … DO UPDATE`), and
the email-verification/password-reset token consume (REC-06) are each a single
`session.execute(...)` call whose SQL is the atomic conditional statement — proved twice per
case: an 8-thread `threading.Barrier` concurrency test (real race, lets exactly one claim
through) **and** a deterministic structural companion test
(`inspect.getsource`, asserts exactly one `session.execute`, no `select`/`session.query`/
`session.get` anywhere in the function) — the structural test exists because the concurrency
test alone was measured to only catch a reverted naive SELECT-then-UPDATE in ~1/30 runs
(REC-04's own before/after measurement), not reliably enough on its own. **Alternatives
considered:** a `SELECT ... FOR UPDATE` row lock followed by a separate `UPDATE` (rejected —
still two statements, still a TOCTOU window if anything between them can commit, and a
behaviourally equivalent but structurally weaker pattern the project's own convention
explicitly rules out). **Consequences:** this is now the template every future single-use
token/counter in this project is expected to follow (recorded in THREAT_MODEL §5.5 explicitly
as a template for REC-05/REC-06 before either existed) — a new feature with a "claim once"
semantic should write the atomic conditional `UPDATE` first, not retrofit it after a DA
review finds the race.

#### P12-D10 — Bearer/JWT/CORS removed entirely (T12.5.3, `9ecc692`)

**Decision:** The legacy Bearer authentication path kept (narrowed, not removed) through 12a
as its own rollback path is now gone completely: `HTTPBearer`, the Bearer fallback inside
`get_current_user`, the JWT in `TokenResponse`, and `CORSMiddleware` are all removed from
`rag_app.api.app`/`rag_app.api.auth` — confirmed by direct reading and the two
`test_cors_is_removed_entirely_*` tests. Every authenticated call is now cookie + CSRF only,
through the BFF. **Alternatives considered:** keeping Bearer as a permanent secondary auth
path for tooling/scripts (rejected — it is the exact mechanism F1 existed to close, and every
internal tool that needed it has been converted to the cookie+CSRF pattern, see
`scripts/restart_check.sh`/`erasure_scale.py`). **Consequences:** this is what makes the old
12a rollback claim in "## Rollback" above stale from 12b onward (DA-12bD-2, fixed there); it
is also the precondition for the backend's ingress going internal-only in T12.6b.5 (nothing
outside the BFF can authenticate to it at all once this landed, so there is nothing left for
an internal-only ingress to break for a legitimate caller).

#### P12-D11 — Route walker: every route declares auth + permission + rate limit, or is public by allow-list (T12.5.2, DEC-023, REC-08)

**Decision:** `backend/tests/security/test_route_walker.py` walks the real app's full
recursive FastAPI dependency tree and asserts every registered route either (a) declares a
permission (via `rag_app.authz.DECLARED_PERMISSION_ROUTES`, never a bare "has auth" check)
and a rate limit (`rag_app.api.deps.RATE_LIMIT_DEPENDENCIES`), or (b) is on a short, explicit
public allow-list (register/login/verify/health/CSP-report — routes that by design have no
session to check a permission against yet). Two companion tests prove the rule has teeth, not
just passes trivially: a toy route with none of the three declarations fails the walker, and a
route with only auth declared (missing permission + rate limit) still fails. This closed a
real, pre-existing gap: `/chat`, `/chat/stream`, `/conversations*`, `/auth/me`,
`DELETE /account` and the MFA enrol/confirm/step-up routes had auth but no declared permission
and, for most, no rate limit at all. **Alternatives considered:** a hand-maintained checklist
of "routes that must have X" reviewed manually at each PR (rejected, DEC-023 — the user's own
observation that a structural, generated-from-the-real-registry test cannot silently go stale
the way a parallel document can). **Consequences:** every new route added anywhere in the
backend is checked by this test automatically; no app-level `app.mount()` exists anywhere
(confirmed) and `/metrics` is served on a separate internal port, never a FastAPI route at
all, so neither is a blind spot for the walker to miss.

#### P12-D12 — Post-deploy/demo health through the frontend's BFF, never a backend FQDN (T12.5.6, DEC-005)

**Decision:** `/api/health` (already on the BFF allow-list since 12a's DEC-005/006) is now
the **only** health check this project's own tooling uses — the CD pipeline, `demo-mode.sh`,
and `restart_check.sh` all resolve the frontend's URL, never the backend's, for a liveness
probe (`backend/tests/test_cd_pipeline.py::
test_backend_health_check_goes_through_the_frontends_bff_not_its_own_fqdn`,
`backend/tests/test_demo_mode.py::
test_on_health_checks_through_the_frontends_bff_not_a_backend_fqdn`). **Alternatives
considered:** keeping a direct backend-FQDN health probe for CD/demo tooling (rejected — the
backend will have no public FQDN to poll at all once T12.6b.5 makes ingress internal; writing
the tooling against the BFF now means nothing needs to change again at that promotion step).
**Consequences:** the private local smoke tool (`rag_ia/tools/smoke.py`, not part of this
repo) still hardcodes the backend FQDN and needs the same rewrite before T12.6b.5 — tracked as
an orchestrator follow-up item, not a gap in this project's own test suite.

#### P12-D13 — Exposed-files check (T12.5.7, DEC-023, REC-09)

**Decision:** `scripts/azure/exposed_files_check.sh` + `backend/tests/security/
test_exposed_files.py` assert, unconditionally (no DB needed), that the backend never serves
real content for `/.git/HEAD`, `/.git/config`, `/.env`, `/.env.local`, a `.next/` server file,
a `*.map` source map, `/backend/`, or `/docker-compose.yml` — the backend has no static file
serving at all, so this is a structural guarantee, not a probabilistic scan. The helper's own
pass/fail logic is proven against a throwaway HTTP server (fails closed on one exposed path,
fails closed on every path exposed, never prints a response body) so the check tool itself
cannot silently become a false-negative. **Alternatives considered:** a one-off manual curl
checklist at promotion time (rejected, DEC-023 — same reasoning as the route walker, a reusable
automated check that also runs in the Azure smoke beats a human remembering to run curl once).
**Consequences:** the same script is reused unmodified by the Azure smoke at T12.6b.6.

#### P12-D14 — `docs/security/SECURITY_REVIEW.md` (T12.5.8)

**Decision:** A new top-level security review document summarising 12a+12b's threat model,
decisions and residual/accepted gaps for a reader who does not want to read this whole ADR —
written to describe the devil's-advocate review process and this project's private debt
register **generically** (matching `docs/DEFINITION_OF_DONE.md`'s existing "private status
record" convention), after an earlier draft of it named those private files directly and was
caught by the `private-names` gate step. **Alternatives considered:** folding this content
into the THREAT_MODEL or this ADR instead of a separate document (rejected — both already
serve different audiences/purposes; a short, standalone review doc is easier to keep current
and to point an external reader at). **Consequences:** none beyond the one-time private-names
fix above; the document and this ADR should be kept consistent on any future change to the
decisions summarised here.

### Consequences (12b, overall)

F3/F5/F6/F10/F11 are closed; DEBT-034 narrows (the email-HMAC and signed-client-IP axes both
apply regardless of ingress) but formally stays open until T12.6b.5's internal ingress; the
in-memory, single-replica limiters (lockout, email abuse) stay an accepted gap under
DEBT-010/RISK-03 until any future multi-replica scale-out. 12b adds migration **0007**
(`user_mfa`, `login_lockouts`) — expand-only, same discipline as 0006; a rollback to a
pre-0007 backend never reads either new table, same reasoning ADR phase 11 already
established for 0006. The remaining 12b work before promotion is entirely T12.6b (internal
ingress, Azure smoke, tag) — no further code-level decision is expected in this ADR before
that.
