# ADR phase 12 — Security baseline: sessions, BFF, CSRF, headers/CSP

- **Status:** **12a** in progress (branch `phase-12a-sessions-bff`); this ADR records the
  **12a part** only (12.0 threat model, 12.1 sessions/BFF/CSRF, 12.2 security headers +
  surface reduction). **12b** (RBAC, MFA, email verification, lockout, backend ingress →
  internal) is a separate promotion and gets its own decisions appended here when it lands.
- **Related:** [`docs/security/THREAT_MODEL.md`](../security/THREAT_MODEL.md) (STRIDE +
  vulnerability catalogue this ADR's decisions close entries against), [TRD §8.1](../TRD.md)
  (implementation summary), [ADR phase 11](adr_phase11_stability.md) (migration discipline
  0005 → **0006**, the first migration this phase adds).
- **Findings closed:** F1 (JWT in `localStorage`, no server-side logout), F2 (no security
  headers), F4 (rate limits keyed by the shared Azure connection address), F7 (`/docs`
  public), F8 (CORS `*` methods/headers) — see THREAT_MODEL §"Findings verified in the code".
  F3/F5/F6/F10/F11 are 12b's.

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
    enforcing**", not "zero violations in report-only mode". **As of this ADR's 12a-E
    Phase 1 (code + unit tests, no browser/build available), `CSP_MODE` is still
    `"report-only"`** — the walk and the flip are 12a-E Phase 2 work, done once a browser and
    a build are available; this paragraph is updated (or the ADR's successor note is) once
    that lands.
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

## Rollback

- **Images roll back; the schema does not** (same principle as ADR phase 11). Migration
  `0006` (`sessions` + `users.role`) is expand-only; the pre-12a backend image never reads
  either new table/column, so a rollback to the previous backend digest still works against
  the post-0006 schema.
- The **old, pre-BFF frontend** still authenticates against the **new** backend during 12a:
  `auth.py` still returns `TokenResponse(access_token=...)` on register/login (Bearer path
  unaffected) and `CORSMiddleware` still allows the frontend's origin (narrowed, not
  removed) — so a rollback to the old frontend digest needs no backend change. `BACKEND_URL`/
  `CLIENT_IP_HMAC_KEYS`/`CSRF_SECRET` can stay set on Azure after a rollback; the old code
  simply ignores the ones it doesn't use.
- A rollback to a **pre-12a-E** frontend/backend digest (before this ADR's headers/CSP/CORS
  narrowing/docs-gating) is also safe in the other direction: none of 12a-E's changes are
  stored state — headers, CSP mode and the docs-gating condition are all read fresh from
  config/code on every request, nothing persists them.

## Successor note (for 12b)

12b removes the legacy Bearer path entirely (CORS removed, not just narrowed), makes the
backend ingress internal-only (closing DEBT-034 for good), adds RBAC + admin MFA (migration
0007), real SMTP verification (ACS) with per-account lockout, and the T12.4.7 email
send/failure counters DEC-013 deferred to. Those decisions get their own section appended
here (or a new ADR, if the orchestrator decides the split warrants one) when 12b starts.
