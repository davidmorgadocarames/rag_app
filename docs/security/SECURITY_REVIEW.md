# SecRAG — Security review (portfolio summary)

> Written for a reader who wants a quick, honest account of this project's security work
> without reading the full catalogue. For the complete STRIDE analysis, trust boundaries and
> the test-ID-backed vulnerability catalogue, see
> [`docs/security/THREAT_MODEL.md`](THREAT_MODEL.md) — this document is a summary of it, not
> a replacement. **No exploit payloads or working proof-of-concept code appear here or in
> any linked document** (`DEFINITION_OF_DONE.md` "Universal norms").

## 1. Scope

This review covers the application as of Phase 12b: the FastAPI backend, the Next.js
frontend/BFF, the Postgres schema and the GitHub Actions CI/CD pipeline that deploys both to
Azure Container Apps. It does **not** cover:

- the agentic MCP tool surface (Phase 17 — not built yet);
- the enterprise multi-tenant/department-role features (Phase 21 — the current role model
  is `user`/`admin` only, built to extend without a route-level rewrite,
  `docs/security/THREAT_MODEL.md` API-5);
- a formal penetration test or a third-party audit — this is a solo project's own review,
  using automated tooling (OWASP ZAP baseline) and structured self-review (the devil's-
  advocate pass every block below went through), not an independent assessment;
- prompt-injection hardening beyond today's single-free-text-field surface (LLM-01/LLM-07,
  planned Phase 14/15.4) and corpus-poisoning defences (LLM-04, planned 15.4.5).

## 2. Method

1. **Threat model** (`docs/security/THREAT_MODEL.md`): a data-flow diagram, nine trust
   boundaries, a STRIDE pass per boundary, and a vulnerability catalogue cross-referenced
   against three public lists — OWASP Top 10:2025, OWASP API Security Top 10:2023, and the
   OWASP Top 10 for LLM Applications 2025 — plus this project's own code-review findings
   (F1–F11, found 2026-09-26).
2. **A gate step that enforces the catalogue stays honest** (`threat-catalogue`): every
   entry needs a phase and a test ID; a `mitigated` entry's test ID must resolve to a real,
   existing test function, or the gate fails. A catalogue entry can say "done" only if a
   test backs it — not as a one-off check, but on every push.
3. **Automated tests**, run on every push (`pytest`, `security-suite`, `db-tests`) and
   before every promotion (`--full`, adding `e2e`, `security-headers`, `zap-baseline`):
   unit, integration and a few genuine multi-thread concurrency tests (proving a no-TOCTOU
   claim under real contention, not just two sequential calls — see X10 below).
4. **OWASP ZAP baseline** (`zap-baseline` gate step, official image, digest-pinned):
   passive scan against the running stack; no High findings without a documented exception.
5. **A route walker** (T12.5.2, `backend/tests/security/test_route_walker.py`): every route
   the backend serves must declare an auth dependency, a permission (never a role string)
   and a rate limit, or be on a closed, named public allow-list — enforced automatically, so
   a new route cannot silently ship without being reviewed against this rule.
6. **A devil's-advocate review after every block of work**: a second, read-only pass over
   the diff and the gate evidence against the Definition of Done and this threat model,
   before the user decides what (if anything) needs a follow-up. Findings that were
   accepted as deferred or as a risk are tracked with an ID in the project's private debt
   register and cited below.

## 3. Findings

### 3.1 Code-review findings (F1–F11), all closed

Found in a single pass on 2026-09-26, before the 12a/12b security work started. Every one
is closed; see `docs/security/THREAT_MODEL.md` for the test proving each.

| ID | Finding | Closed in | How |
|---|---|---|---|
| F1 | JWT kept in `localStorage`, readable by any XSS, no server-side logout | 12a, fully removed 12b (T12.5.3) | Server-side sessions behind a `__Host-`, `HttpOnly` cookie; the legacy Bearer/JWT path was removed entirely in 12b, not just made secondary. |
| F2 | No security headers on frontend or backend responses | 12a (T12.2.1–T12.2.3) | HSTS, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, a per-request nonce CSP (enforcing, zero violations in a real-browser Playwright walk). |
| F3 | User enumeration via the register/login response shape and timing | 12b-B (T12.4.1/T12.4.4) | Generic 202 on register whatever the outcome; the "already registered" and "new account" code paths cost the same (no upfront existence check); constant-time login (a real argon2 verify on every path). |
| F4 | Rate limits keyed by IP, defeated once every request shares one Azure-internal address | 12a-C (T12.1.6), fully closes 12b-P (T12.6b.5) | A signed client-IP header from the BFF; accepted gap until the backend goes internal-only (DEBT-034). |
| F5 | No login lockout | 12b-B (T12.4.2) | Exponential back-off, keyed by a salted hash of the email (not the account), atomic counter (X10). |
| F6 | No roles / access control model | 12a–12b (T12.3.2–T12.3.6) | A deny-by-default permission map (never a role-string comparison), an admin bootstrap CLI as the only writer of `role`, admin routes requiring a fresh MFA step-up, and a client-tampering test suite covering mass assignment, header/cookie tricks and direct backend calls. |
| F7 | `/docs`, `/redoc`, `/openapi.json` public in production | 12a (T12.2.4) | Gated to `ENV=dev`; a 404 (not an auth-gated route) with `ENV=prod`. |
| F8 | CORS wide open (`*` methods/headers) | narrowed 12a, **removed entirely** 12b (T12.5.3) | The BFF-based frontend never triggers a browser CORS preflight at all; the old frontend's rollback need for CORS no longer exists once its Bearer path was removed the same block. |
| F9 | Email verification link returned in the API response or logged | 11a / 12a (T12.1.11) | Removed from the response and the logs entirely; only ever sent by email (Mailpit locally, ACS SMTP on Azure). |
| F10 | No password policy | 12b-B (T12.4.3) | ≥ 12 characters, the NCSC/SecLists common-password list, and a check against the account's own email. |
| F11 | Login allowed before email verification | 12b-B (T12.4.4) | `require_email_verification` defaults on with `ENV=prod`; an unverified account's login is refused. |

### 3.2 Design decisions written down as they were made

Two cross-cutting rules, adopted 2026-11-09 (DEC-023) and applied to every new mitigation
since:

- **X10 — no TOCTOU.** A decision that depends on a database row's state is never a SELECT
  to check it followed by a separate UPDATE; it is one atomic statement. Every mitigation
  that depends on this (recovery-code claims, login lockout, email/password-reset token
  consume, email-abuse counters) has both a genuine multi-thread concurrency test and a
  deterministic structural test (so a regression back to the unsafe shape is caught even
  when the probabilistic concurrency test would not reliably catch it — REC-04's own
  measurement: about 3.3% failure rate over 30 runs for the unsafe version, versus the
  structural test's 100%).
- **X11 — cheap staging for migrations.** Before any promotion that ships a schema
  migration, the latest Azure backup is restored into a throw-away local Postgres and the
  migration (plus its downgrade) is rehearsed there first, before touching the real
  database.

### 3.3 Accepted risks (deferred, not fixed, and why)

Decided by the project owner, each with a `DEBT-0xx` tracking row and a re-check date —
never silently dropped. Full detail in `docs/security/THREAT_MODEL.md` §5.4.

| Risk | Why accepted | Re-checked |
|---|---|---|
| Azure OpenAI GlobalStandard data residency: a question may be processed outside the EU | The EU-only DataZoneStandard quota is 0 in every EU region for this account; disclosed in the product's own privacy text (`docs/PRD.md`, `README.md`) | Phases 14, 15.4 |
| PostgreSQL firewall allows any Azure tenant's traffic | No VNet in the standard Container Apps environment at this budget; mitigated by long random passwords, least-privilege roles, required TLS | Phase 15.0 |
| Reranker concurrency cap (2) without autoscaling | Single-replica budget; queueing under load is a latency cost, not a security one | Phase 16.3 (KEDA) |
| Demo mode keeps apps briefly awake past intended use; the hourly guard can lag | Cost-bounded (a few cents/hour); the guard only ever scales down, never up | Every phase close |
| Login-lockout keying collapses to email-only if a caller reaches the (still-public, until 12b-P) backend directly, bypassing the BFF's signed IP | Not worse than the pre-12a pure-IP keying; a real-IP second axis already applies to every caller that does go through the BFF | Closes at 12b-P (T12.6b.5, backend ingress internal) |
| Email-abuse recipient backstop can still be exhausted by a **coordinated many-IP** flood (each IP staying under its own small per-recipient share) | A single-replica, in-memory rate limiter has no cross-process view; closing the many-IP case needs a shared store (e.g. Redis), out of scope before the backend runs more than one replica | Phase 16.3 |

## 4. What this review does not claim

- No independent penetration test or third-party audit has been performed.
- The OWASP ZAP baseline scan is **passive** (it does not attempt exploitation) and runs
  against the local gate stack, not the live Azure deployment.
- "Mitigated" in the catalogue means a specific, named test proves the described behaviour
  today — it does not mean the broader category of risk is eliminated forever; a regression
  would need the test itself to be weakened or deleted, which the `threat-catalogue` gate
  step would also need to miss.
- Phases 15, 17 and 21 (deeper RBAC/audit, the agentic MCP tool surface, enterprise
  multi-tenancy) each reopen this document's scope and add their own catalogue entries —
  this review will be revised again at each of those phase closes.

## 5. Review history

Reviewed by the devil's-advocate pass of each block that touched security-relevant code;
see the project's private status record (block sections) for the per-block findings and
the user's decisions on each. This document itself is reviewed by the devil's-advocate
agent before the Phase 12 close (T12.5.8).
