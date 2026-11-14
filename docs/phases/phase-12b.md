# Phase 12b — Threat model + security baseline, part 2 (report)

- **Closing:** 2026-11-14 · **Tag:** `phase-12b` on `62451dc` (the deployed commit).
- **Promoted to Azure:** 2026-11-13/14 (`main` `c958992` → `62451dc`, fast-forward, PR #19).
- **Cost:** ≈ €0.002 posted (cap €1) — see Cost below.
- **Decisions:** [ADR phase 12](../adr/adr_phase12_security_baseline.md) (12b section, P12-D4…D15) ·
  **Threat model:** [`docs/security/THREAT_MODEL.md`](../security/THREAT_MODEL.md) ·
  **Review summary:** [`docs/security/SECURITY_REVIEW.md`](../security/SECURITY_REVIEW.md).

## Why this phase existed

12a moved the browser to server-side sessions behind a Backend-For-Frontend proxy, but five
findings of the threat model were still open: login timing and response shape revealed whether
an account existed (F3), there was no lockout (F5), no roles (F6), no password policy (F10) and
no real e-mail verification (F11). The legacy Bearer path and a narrowed CORS policy were still
alive as the 12a rollback path, and the backend still had public ingress, so a caller that
skipped the BFF was rate-limited on the one shared Azure address (DEBT-034). 12b closes all of
that: roles with admin MFA, constant-time authentication, lockout, a password policy, real
e-mail through Azure Communication Services, password reset, the removal of Bearer and CORS,
and an internal-only backend.

## What was done

| Block | What | Result |
|---|---|---|
| A | Roles, admin MFA, early fixes | Role → permission map (`require_permission`, never a role-string comparison) built for future department roles; admin TOTP MFA with the secret encrypted at rest, hashed single-use recovery codes and a step-up that rotates the session; client-side QR code for enrolment; `/account/security` and `/admin` pages; admin bootstrap CLI (`python -m rag_app.admin create`, interactive, policy-checked password); a client-tampering security suite. Early tasks: signed client IP window 30 s → 300 s bound to method + path with a `verified/fallback/stale/forged` counter (DEBT-038), development-only dependency bumps (DEBT-039), typo-tolerant small talk so "holaa" or "graicas" no longer trigger the full RAG pipeline (DEBT-040). |
| B | Authentication hardening | Constant-time login; register takes the same code path and cost for new and existing addresses, and `register` / `resend-verification` / `forgot-password` always answer a generic 202; password policy (12–256 characters, no email local part, NCSC top-100k list, P12-D4); lockout keyed by `HMAC(secret, email)` with exponential back-off, cleared on reset and deleted on erasure; e-mail verification required with `ENV=prod`; frontend `/verify` and `/reset` pages that POST the token (so a mail scanner pre-fetching the link cannot consume it); Mailpit in the e2e gate step. |
| C | Password reset + e-mail abuse limits | Migration **0007** part 3 (`password_reset_tokens`); every single-use token or counter consumed in one atomic statement (no SELECT-then-UPDATE); four-bucket e-mail abuse limiter (per IP → per recipient + IP → per recipient as a backstop → global); the recipient + IP bucket (DEC-026) caps one source's share of a victim's e-mails so a trickle attacker cannot keep the victim's reset e-mails drained. |
| D | Surface reduction | Bearer/JWT and CORS removed entirely; a route walker test fails any route that does not declare auth + permission + rate limit or sit on the public allow-list; post-deploy and demo health checks go through the frontend's BFF, never a backend address; an exposed-files check (`/.git/`, `/.env*`, source maps, server files → 404); `SECURITY_REVIEW.md`; THREAT_MODEL, TRD, PRD, README and Definition of Done updated. |
| P | Promotion | Backup + a rehearsal of 0007 (upgrade → downgrade → upgrade) on a restored copy of the latest Azure backup; Azure Communication Services e-mail provisioned; new secrets set before the merge; merge, CD, migration 0007; admin account created; backend ingress switched to internal; Azure smoke. |

## Deviations from the plan (accepted by the user)

- **A dependency fix before the merge (DEC-027).** The first pre-merge `--full` gate failed on
  `opentelemetry-api` 1.45.1 (CVE-2026-126360, a low-severity `traceparent` parsing DoS,
  published after the last block's gate). Bumped to 1.45.2 and re-gated; nothing reached
  `main` before that.
- **Smoke verification links pasted by hand.** The plan had the smoke tool read the
  verification e-mail over IMAP. The provider refused an app password on the new test mailbox,
  so the smoke tool asks the operator to paste the link (hidden input, never printed); e-mail
  delivery was confirmed by the provider's send status plus receipt.
- **Wrong SMTP host on the first try.** The SMTP host was first set to a name that does not
  resolve; register still answered the generic 202 and only the backend log showed
  `email not sent`. Corrected to `smtp.azurecomm.net` with one setting update before any
  e-mail was spent — a reminder that a generic 202 hides delivery failures from the client by
  design.
- **The migration rehearsal stopped at the schema.** 0007 was rehearsed on a restored backup
  (row counts, all four new tables, downgrade and re-upgrade) without starting the app on it,
  because that needs the production data master key; the app-level check was the real smoke.
- **Secret length checks inside the replica skipped.** The interactive `exec` needs a terminal;
  the proof that the new lockout secret resolved is that the 12b backend starts at all (it
  refuses to start without it).
- **CD rebuilt the Ollama image** although nothing in it changed, so the rollback set lists the
  previous Ollama digest too.

## Gate evidence

- `gate.sh --full` **PASS 697 s (31/31 steps)** on `62451dc` before the merge, and **PASS 607 s**
  in the pre-push hook at the `main` push; `secrag/gate-full` published.
- Earlier on the branch: block D `--full` PASS 541 s (`4d92473`); the first pre-merge run on
  `08c0c53` failed in `dependency-audit` (above).
- CI **7/7** required checks on `62451dc` (run `43523418788`).
- Diff `c958992..62451dc`: 40 commits, 106 files, +7663 / −786.

## Azure promotion evidence

- CD run `43524910770` green; migrate Job applied **0007** (`user_mfa`, `mfa_recovery_codes`,
  `login_lockouts`, `password_reset_tokens`).
- Deployed digests (prefixes): backend `sha256:7090d2bede79…`, frontend `sha256:1ab1c011550c…`,
  Ollama `sha256:ea92a60e9693…`, jobs `sha256:9d4ea3dfe081…`.
- Rollback (12a) digests: backend `sha256:0a92f59d40cdf165d419bdf11f1bf4227e5df6e6192653f886b6f6d3542c3c14`,
  frontend `sha256:452747f7d69344ba63f2e57577d6c0d0f4d20245d13f983bc3ce5b12129915f8`, Ollama
  `sha256:115d023adf20e2d1bcdc2a73e04b68564de6297c9989352a4f5d82bd6c19e9d4`, jobs
  `sha256:7e49b11244bc7db30c850b48ab50e9fd65202f114d67ee0811626a1e551fc04d`. Order: backend
  ingress back to external and the frontend's `BACKEND_URL` back to the public address first,
  then all images together; the schema stays (0007 is expand-only) — see the ADR.
- **Smoke PASS:** headers and an enforcing CSP on `/`, `/login`, `/api/health`; `/api/docs`,
  `/api/metrics`, `/api/openapi.json` and the exposed-files list → 404; the backend's public
  address → 404 from the internet; register → verification e-mail → verify → login through the
  BFF; a chat question through the BFF with 3 citations (cold first byte 51.8 s); a normal user
  → `/admin` 403; the admin had to enrol TOTP (QR) and pass a 6-digit step-up before `/admin`;
  lockout after 6 failed logins (429 even with the right password, longer on the next round);
  forgot-password → reset → login with the new password, which also cleared the lockout;
  account erasure through the browser → purge Job processed 1 erasure (user, conversation,
  messages, verification and reset tokens); no signed-IP fallback after a cold start; every app
  back to 0 replicas. Three e-mails used, the budget for a promotion.

## Cost

Measured (Azure Cost Management, `ActualCost`, read-only, queried 2026-11-14): 2026-11-12
**€0.0000**, 2026-11-13 **€0.0009** (Azure OpenAI tokens), 2026-11-14 **€0.0007** so far (three
ACS e-mails; the day is not fully posted). Container Apps stayed inside November's free grant.
**12b ≈ €0.002**, cap €1. Azure Communication Services is pay-per-use and costs nothing idle.

## Changed docs

[ADR phase 12](../adr/adr_phase12_security_baseline.md) (12b section; decision numbering aligned
with the plan — P12-D4 is the password policy, the 12b decisions are P12-D5…D15; promotion,
rollback and cost), [THREAT_MODEL](../security/THREAT_MODEL.md) (F3/F5/F6/F10/F11 mitigated),
[SECURITY_REVIEW](../security/SECURITY_REVIEW.md) (new), [TRD](../TRD.md), [PRD](../PRD.md),
[README](../../README.md) (new secrets, password-list attribution),
[Definition of Done](../DEFINITION_OF_DONE.md) (new gate steps).

## Known follow-ups

- **DEBT-041** — the data master key cannot be rotated today: it wraps every per-user data key
  and the MFA secrets, and the stored fingerprint makes the app refuse any other key. Needs a
  re-wrap tool (Phase 15).
- **DEBT-010** — the lockout and e-mail abuse limiters are per replica; fine at max 1 replica,
  to revisit before any scale-out (Phase 16).
- **DEBT-003** — the `postcss` copy bundled inside Next.js stays a dated, build-time-only
  exception until the Next.js major upgrade (Phase 15).
- **DEBT-036** — the Azure OpenAI model version in use retires on 2027-04-14; a deliberate,
  evaluated migration is planned in Phase 14.
- **DEBT-002** — two orphan smoke accounts from Phase 11b, to be erased with the admin erasure
  panel in Phase 13.
- The SMTP credential (an Entra client secret) expires on 2027-12-31, after the planned end of
  the demo; rotating it is a no-downtime procedure.
