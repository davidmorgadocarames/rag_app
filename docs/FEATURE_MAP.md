# Feature Map

> **GENERATED** by `python -m rag_app.features map` from `backend/src/rag_app/features.py`'s `REGISTRY`/`FEATURE_DOCS` + `docs/APP_FLOW.md`. Never hand-edit this file -- regenerate it instead; `backend/tests/test_feature_map.py` fails the build the moment this file drifts from the generator.

## How to verify a feature

1. Find the feature's row below (id, page, prerequisites, e2e spec(s)).
2. Run `scripts/dev/verify.sh <feature-id> [--base-url URL]` (T13.17.3): it looks up this same map, brings up what the feature needs locally (or targets `--base-url`), runs the covering e2e spec(s) with Playwright, and writes evidence (screenshots + log) to a folder it prints.
3. Exit code: `0` PASS, `1` FAIL, `2` BLOCKED (no covering spec for this id, or a missing prerequisite -- e.g. nothing reachable at `--base-url`).
4. A feature with no e2e spec (see the coverage table below) can still be checked with its own health/quality probe: `python -m rag_app.devtools.registry_check`.

## Features by group

### Stack

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `stack-health-endpoint` | Liveness endpoint (/health) | 11a | enabled | N/A -- infrastructure (no user-facing page) | `GET /health` registered | — |
| `stack-coldstart-warmup` | Cold-start warm-up (background Ollama embed, lifespan + GET /wake) | 13-J4 | enabled | /login, /chat (background) + GET /wake (BFF-proxied) | `GET /wake` registered | `wakeup.spec.ts`, `wakeup-slow.spec.ts`, `wakeup-pipeline.spec.ts` |
| `stack-postgres` | Postgres | 11a | enabled | N/A -- infrastructure | `probe_postgres` (custom) | — |
| `stack-migrations` | Alembic migrations | 11a | enabled | N/A -- infrastructure | `probe_migrations` (custom) | — |
| `stack-embeddings` | Embeddings (Ollama bge-m3) | 11a | enabled | N/A -- infrastructure | `probe_embeddings_configured` (custom) | — |
| `stack-reranker` | Cross-encoder reranker (shared, quantized) | 11b | enabled | N/A -- infrastructure | `probe_reranker_configured` (custom) | — |
| `stack-llm-provider` | LLM provider (Ollama / Azure OpenAI) | 10 | enabled | N/A -- infrastructure | `probe_llm_provider_configured` (custom) | — |
| `stack-prometheus` | Prometheus metrics (in-process registry) | 11b | enabled | N/A -- infrastructure (admin Latency panel reads it) | `probe_prometheus` (custom) | — |

**`stack-health-endpoint`** -- Liveness endpoint (/health)

- Prerequisites: none
- User steps: GET /health
- Observable result: 200 with a liveness payload
- Key source files: `backend/src/rag_app/api/app.py`
- Docs: `docs/TRD.md`

**`stack-coldstart-warmup`** -- Cold-start warm-up (background Ollama embed, lifespan + GET /wake)

- Prerequisites: none for /wake; an account for /login, /chat
- User steps: load /login or /chat after >=5 min idle (Azure scale-to-zero) or call GET /wake
- Observable result: the 'Waking the server up...' banner clears once the backend responds; secrag_ollama_warmup_total increments
- Key source files: `backend/src/rag_app/warmup.py`, `frontend/lib/wakePing.ts`, `frontend/lib/wakeUp.ts`
- Docs: `docs/TRD.md`

**`stack-postgres`** -- Postgres

- Prerequisites: a live DATABASE_URL
- User steps: SELECT 1 through the probe
- Observable result: probe status ok/degraded/down in the admin Overview panel
- Key source files: `backend/src/rag_app/db/session.py`
- Docs: `docs/BACKEND_SCHEMA.md`

**`stack-migrations`** -- Alembic migrations

- Prerequisites: a live, migrated DATABASE_URL
- User steps: alembic upgrade head; probe reads alembic_version
- Observable result: probe ok once alembic_version is populated
- Key source files: `backend/src/rag_app/db/models.py`, `backend/alembic/`
- Docs: `docs/BACKEND_SCHEMA.md`

**`stack-embeddings`** -- Embeddings (Ollama bge-m3)

- Prerequisites: OLLAMA_HOST + EMBED_MODEL configured
- User steps: any chat question (first embed call)
- Observable result: probe reports the configured embed model; a real call succeeds in chat
- Key source files: `backend/src/rag_app/embeddings.py`
- Docs: `docs/adr/adr_phase11_stability.md`

**`stack-reranker`** -- Cross-encoder reranker (shared, quantized)

- Prerequisites: RERANKER_MODEL/RERANKER_REVISION configured
- User steps: any security question (hybrid retrieval always reranks before generation)
- Observable result: probe reports the configured model, '(loaded)' once warmed
- Key source files: `backend/src/rag_app/reranking.py`
- Docs: `docs/adr/adr_phase11_stability.md`

**`stack-llm-provider`** -- LLM provider (Ollama / Azure OpenAI)

- Prerequisites: LLM_PROVIDER + its own endpoint/model configured
- User steps: any chat question
- Observable result: probe reports 'configured: ollama' or 'configured: azure_openai'
- Key source files: `backend/src/rag_app/llm.py`
- Docs: `docs/adr/adr_phase10_azure.md`

**`stack-prometheus`** -- Prometheus metrics (in-process registry)

- Prerequisites: none
- User steps: GET /metrics, or open the admin Latency panel
- Observable result: Prometheus text exposition with the chat-stage histograms
- Key source files: `backend/src/rag_app/metrics.py`
- Docs: `docs/adr/adr_phase13_dashboard.md`

### RAG

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `rag-hybrid-retrieval` | Hybrid retrieval (pgvector + BM25, RRF fusion) | 11a | enabled | /chat | `rag_app.retrieval` importable | `auth.spec.ts` |
| `rag-rerank` | Cross-encoder rerank | 11b | enabled | /chat | `rag_app.reranking` importable | `auth.spec.ts` |
| `rag-generation-groundedness` | Generation + groundedness check | 11a | enabled | /chat | `rag_app.generation` importable | `auth.spec.ts` |
| `rag-abstention` | Abstention (no reliable source) | 11a | enabled | /chat | `rag_app.generation` importable | — |
| `rag-chitchat` | Chit-chat (incl. typo-tolerant small talk) | 12b | enabled | /chat | `rag_app.generation` importable | — |
| `rag-sse-token-usage` | Streaming answers (SSE) + token usage | 11a | enabled | /chat | `POST /chat/stream` registered | `auth.spec.ts` |
| `rag-agentic-router` | Agentic router (query-rewrite retry, CRAG-lite) | 11b | retained-not-adopted | N/A -- retained-not-adopted, not wired into /chat | `rag_app.agentic` importable | — |

**`rag-hybrid-retrieval`** -- Hybrid retrieval (pgvector + BM25, RRF fusion)

- Prerequisites: auth; seeded corpus
- User steps: ask a security question
- Observable result: citations reference real, relevant chunks (pgvector + BM25, RRF-fused)
- Key source files: `backend/src/rag_app/retrieval.py`
- Docs: `docs/TRD.md`

**`rag-rerank`** -- Cross-encoder rerank

- Prerequisites: auth; seeded corpus
- User steps: ask a security question
- Observable result: the top cited chunk is the cross-encoder's top pick, not just RRF's
- Key source files: `backend/src/rag_app/reranking.py`
- Docs: `docs/adr/adr_phase11_stability.md`

**`rag-generation-groundedness`** -- Generation + groundedness check

- Prerequisites: auth; seeded corpus
- User steps: ask a security question
- Observable result: an answer grounded in the retrieved chunks, or abstention when it is not
- Key source files: `backend/src/rag_app/generation.py`
- Docs: `docs/TRD.md`

**`rag-abstention`** -- Abstention (no reliable source)

- Prerequisites: auth; a question with no reliable source in the corpus
- User steps: ask something the corpus cannot answer
- Observable result: the dignified 'No reliable source in the corpus' card, no fabricated content
- Key source files: `backend/src/rag_app/generation.py`
- Docs: `docs/TRD.md`

**`rag-chitchat`** -- Chit-chat (incl. typo-tolerant small talk)

- Prerequisites: auth
- User steps: send a greeting/small-talk message (e.g. 'hello', even misspelled)
- Observable result: an instant, non-sourced reply with no retrieval/citations/abstention
- Key source files: `backend/src/rag_app/generation.py`
- Docs: `docs/TRD.md`

**`rag-sse-token-usage`** -- Streaming answers (SSE) + token usage

- Prerequisites: auth
- User steps: send any chat message
- Observable result: conversation -> stage -> token... -> done SSE events; token usage per answer
- Key source files: `backend/src/rag_app/api/app.py`, `frontend/lib/api.ts`
- Docs: `docs/TRD.md`

**`rag-agentic-router`** -- Agentic router (query-rewrite retry, CRAG-lite)

- Prerequisites: none (code kept, not routed)
- User steps: n/a
- Observable result: module importable; not reachable through the product
- Key source files: `backend/src/rag_app/agentic.py`
- Docs: `docs/adr/adr_phase11_stability.md`

### Quality

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `quality-eval-vs-baseline` | Eval vs baseline (golden set) | 11a | enabled | N/A -- CI/offline (admin Quality panel shows the last run's aggregate) | `rag_app.eval.benchmark` importable | — |
| `quality-judge-kappa` | Correctness judge (Cohen's kappa validated) | 11a | enabled | N/A -- CI/offline | `rag_app.eval.judge` importable | — |
| `quality-per-stage-latency` | Per-stage latency benchmark | 11b | enabled | /admin -&gt; Latency panel | `rag_app.eval.latency` importable | `admin-dashboard.spec.ts` |
| `quality-misspelling-probe` | Misspelling tolerance (query typo robustness) | 13 | enabled | N/A -- CI/offline (`registry` gate step) | quality probe: misspelling-tolerance, misspelling-tolerance-variants | — |

**`quality-eval-vs-baseline`** -- Eval vs baseline (golden set)

- Prerequisites: golden set + a live stack
- User steps: run the `eval` gate step, or load the admin Quality panel
- Observable result: pass rate vs baseline/floors
- Key source files: `backend/src/rag_app/eval/benchmark.py`
- Docs: `docs/DEFINITION_OF_DONE.md`

**`quality-judge-kappa`** -- Correctness judge (Cohen's kappa validated)

- Prerequisites: golden set + a live stack
- User steps: run the `eval` gate step
- Observable result: judge agreement (Cohen's kappa) against the human-labelled set
- Key source files: `backend/src/rag_app/eval/judge.py`
- Docs: `docs/DEFINITION_OF_DONE.md`

**`quality-per-stage-latency`** -- Per-stage latency benchmark

- Prerequisites: auth + role=admin + MFA step-up
- User steps: open the admin dashboard's Latency panel
- Observable result: per-stage p50/p95 waterfall from this replica's own histogram
- Key source files: `backend/src/rag_app/eval/latency.py`, `backend/src/rag_app/metrics.py`
- Docs: `docs/adr/adr_phase11_stability.md`

**`quality-misspelling-probe`** -- Misspelling tolerance (query typo robustness)

- Prerequisites: golden set + a live stack
- User steps: ask a question with a single-typo word
- Observable result: the expected document is still retrieved despite the typo
- Key source files: `backend/src/rag_app/features.py`
- Docs: `docs/adr/adr_phase13_dashboard.md`

### Identity

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `identity-registration-verification` | Registration + email verification | 11a | enabled | /signup, /verify | `POST /auth/register` registered | `auth.spec.ts` |
| `identity-login-sessions` | Login + server-side sessions | 11a | enabled | /login | `POST /auth/login` registered | `auth.spec.ts`, `wakeup.spec.ts` |
| `identity-lockout` | Login lockout (exponential back-off) | 12b | enabled | /login | `rag_app.lockout` importable | — |
| `identity-mfa` | MFA (TOTP enrol/confirm + step-up, recovery codes) | 12b | enabled | /account/security | `POST /account/mfa/step-up` registered | `mfa.spec.ts`, `admin-dashboard.spec.ts` |
| `identity-email-verification-reset` | Email verification/reset links (SMTP; ACS SMTP on Azure) | 11a | enabled | /verify, /forgot-password, /reset | `rag_app.emailer` importable | `auth.spec.ts` |
| `identity-password-policy-reset` | Password policy + reset | 12b | enabled | /signup, /reset | `rag_app.password_policy` importable | — |
| `identity-permission-map` | Permission map (require_permission_and_mfa) | 12b | enabled | /admin/* (enforced server-side) | `rag_app.authz` importable | `admin.spec.ts` |

**`identity-registration-verification`** -- Registration + email verification

- Prerequisites: none
- User steps: register with email + password; click the emailed verification link
- Observable result: generic 202 on register; account usable after verification
- Key source files: `frontend/app/login/`, `backend/src/rag_app/api/app.py`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`identity-login-sessions`** -- Login + server-side sessions

- Prerequisites: a verified account
- User steps: open the vault door (ABRIR), enter credentials, press ENTER
- Observable result: redirect to /chat; __Host-secrag_session + __Host-secrag_csrf cookies set
- Key source files: `frontend/components/VaultDoor.tsx`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`identity-lockout`** -- Login lockout (exponential back-off)

- Prerequisites: several failed login attempts for the same account/IP
- User steps: fail login repeatedly
- Observable result: exponential back-off; secrag_lockout_hits_total increments
- Key source files: `backend/src/rag_app/lockout.py`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`identity-mfa`** -- MFA (TOTP enrol/confirm + step-up, recovery codes)

- Prerequisites: auth
- User steps: start enrolment, scan/enter the QR secret, submit the 6-digit code, then step up
- Observable result: a real QR <svg>, 'Two-factor authentication is enabled.', then 'MFA verified.'
- Key source files: `frontend/app/account/security/page.tsx`, `frontend/components/QrCode.tsx`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`identity-email-verification-reset`** -- Email verification/reset links (SMTP; ACS SMTP on Azure)

- Prerequisites: none (reached by email, not a session)
- User steps: follow the emailed link, then submit the page's own form
- Observable result: email delivered (Mailpit locally); generic response either way
- Key source files: `backend/src/rag_app/emailer.py`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`identity-password-policy-reset`** -- Password policy + reset

- Prerequisites: none
- User steps: submit a password that violates the policy
- Observable result: a clear validation error; policy enforced server-side too
- Key source files: `backend/src/rag_app/password_policy.py`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`identity-permission-map`** -- Permission map (require_permission_and_mfa)

- Prerequisites: auth; role=admin; MFA step-up
- User steps: call an /admin/* endpoint without the right role/step-up
- Observable result: 403/redirect to the denied placeholder, never admin content
- Key source files: `backend/src/rag_app/authz.py`
- Docs: `docs/TRD.md`

### Abuse

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `abuse-rate-limiting` | Rate limiting (token bucket) | 11a | enabled | N/A -- backend middleware | `rag_app.ratelimit` importable | — |
| `abuse-signup-risk` | Signup risk scoring (disposable domains + velocity) | 12b | enabled | /signup (server-side decision) | `rag_app.risk` importable | — |
| `abuse-signed-client-ip` | Signed client IP (BFF -&gt; backend) | 12a | enabled | N/A -- BFF -&gt; backend internal header | `rag_app.client_ip` importable | — |
| `abuse-email-abuse-limits` | Email abuse limits (per-IP/recipient/global) | 12b | enabled | N/A -- backend limiter | `rag_app.email_abuse` importable | — |

**`abuse-rate-limiting`** -- Rate limiting (token bucket)

- Prerequisites: repeated requests over the configured bucket
- User steps: exceed the per-user/IP token bucket
- Observable result: 429; secrag_rate_limit_hits_total increments
- Key source files: `backend/src/rag_app/ratelimit.py`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`abuse-signup-risk`** -- Signup risk scoring (disposable domains + velocity)

- Prerequisites: a disposable-domain address or high signup velocity
- User steps: register from a disposable domain, or many times quickly
- Observable result: 403, no account created; secrag_signup_blocked_total increments
- Key source files: `backend/src/rag_app/risk.py`
- Docs: `docs/adr/adr_phase12_security_baseline.md`

**`abuse-signed-client-ip`** -- Signed client IP (BFF -> backend)

- Prerequisites: a request through the Next.js BFF
- User steps: any /api/* call
- Observable result: rate limits key on the real caller, not the shared Azure address
- Key source files: `backend/src/rag_app/client_ip.py`
- Docs: `docs/TRD.md`

**`abuse-email-abuse-limits`** -- Email abuse limits (per-IP/recipient/global)

- Prerequisites: repeated verification/reset emails to the same target
- User steps: trigger many verification/reset emails quickly
- Observable result: further sends refused per-IP/recipient/globally
- Key source files: `backend/src/rag_app/email_abuse.py`
- Docs: `docs/security/THREAT_MODEL.md`

### Privacy

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `privacy-encrypted-conversations` | Encrypted conversations (per-user key) | 6 | enabled | /chat, /history | `rag_app.crypto` importable | — |
| `privacy-asynchronous-erasure` | Asynchronous erasure (crypto-shred + batched purge) | 11b | enabled | /account (self) or /admin (an admin erasing another account) | `DELETE /account` registered | `admin-dashboard.spec.ts` |
| `privacy-deletion-replay` | Deletion replay (restore safety) | 11b | enabled | N/A -- restore-safety, no direct UI | `rag_app.erasure` importable | — |

**`privacy-encrypted-conversations`** -- Encrypted conversations (per-user key)

- Prerequisites: auth
- User steps: send a chat message
- Observable result: message/title rows stored encrypted under the user's own wrapped key
- Key source files: `backend/src/rag_app/crypto.py`
- Docs: `docs/adr/adr_phase06_gdpr_erasure.md`

**`privacy-asynchronous-erasure`** -- Asynchronous erasure (crypto-shred + batched purge)

- Prerequisites: auth (self), or auth + role=admin + MFA step-up (admin erase)
- User steps: Delete my data -> confirm, or the admin Erasure panel's Erase-an-account form
- Observable result: 202 Accepted; crypto-shred immediately; remaining rows purged within 24h
- Key source files: `backend/src/rag_app/erasure.py`
- Docs: `docs/adr/adr_phase06_gdpr_erasure.md`

**`privacy-deletion-replay`** -- Deletion replay (restore safety)

- Prerequisites: a restored backup with pending tombstones
- User steps: restore a backup taken after an erasure
- Observable result: tombstoned rows are deleted again on restore, never resurrected
- Key source files: `backend/src/rag_app/erasure.py`
- Docs: `docs/adr/adr_phase06_gdpr_erasure.md`

### Browser security

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `browser-security-headers` | Security headers (CSP, nosniff, referrer policy, no-store) | 12a | enabled | every page | `rag_app.api.app` importable | `security-headers.spec.ts` |
| `browser-csp-reports` | CSP violation reports (/csp-report) | 12a | enabled | N/A -- browser-sent, no user-facing page | `POST /csp-report` registered | `security-headers.spec.ts` |
| `browser-exposed-files` | Exposed-files check (.git/.env/source maps never served) | 12b | enabled | N/A -- infrastructure | `probe_exposed_files` (custom) | — |

**`browser-security-headers`** -- Security headers (CSP, nosniff, referrer policy, no-store)

- Prerequisites: none
- User steps: load any page
- Observable result: CSP/nosniff/referrer-policy/no-store headers present, zero CSP violations
- Key source files: `backend/src/rag_app/api/app.py`, `frontend/middleware.ts`
- Docs: `docs/TRD.md`

**`browser-csp-reports`** -- CSP violation reports (/csp-report)

- Prerequisites: a real CSP violation in the browser
- User steps: trigger a CSP violation (e.g. an injected inline script)
- Observable result: POST /csp-report accepted; secrag_csp_reports_total increments
- Key source files: `backend/src/rag_app/api/app.py`
- Docs: `docs/TRD.md`

**`browser-exposed-files`** -- Exposed-files check (.git/.env/source maps never served)

- Prerequisites: none
- User steps: request .git/.env/a source map from the live app
- Observable result: never served (backend mounts no StaticFiles app at all)
- Key source files: `backend/tests/security/test_exposed_files.py`
- Docs: `docs/TRD.md`

### Azure-only

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `azure-internal-ingress` | Internal ingress (backend has no public URL) | 12b | enabled | N/A -- Azure-only property, not observable locally | none | — |

**`azure-internal-ingress`** -- Internal ingress (backend has no public URL)

- Prerequisites: Azure Container Apps
- User steps: try to reach the backend's URL directly from outside the environment
- Observable result: unreachable -- internal ingress only, the frontend/BFF is the only public URL
- Key source files: `docs/TRD.md`
- Docs: `docs/TRD.md`

### Admin

| id | name | phase | status | page | health/quality probe | e2e spec(s) |
|---|---|---|---|---|---|---|
| `admin-api` | Admin API (/admin/*, permission + MFA step-up gated) | 12b | enabled | /admin | `GET /admin/ping` registered | `admin.spec.ts`, `admin-dashboard.spec.ts` |
| `admin-defence-lab` | Defence lab (THREAT_MODEL-catalogue probes, CLI + admin button) | 13 | enabled | /admin -&gt; Defence lab tab (local only, LAB_ENABLED) | `GET /admin/lab/runs` registered | `admin-dashboard.spec.ts` |

**`admin-api`** -- Admin API (/admin/*, permission + MFA step-up gated)

- Prerequisites: auth; role=admin; MFA set on the session
- User steps: open /admin
- Observable result: the denied placeholder for anyone else; the real dashboard for an admin
- Key source files: `frontend/app/admin/page.tsx`, `backend/src/rag_app/api/app.py`
- Docs: `docs/adr/adr_phase13_dashboard.md`

**`admin-defence-lab`** -- Defence lab (THREAT_MODEL-catalogue probes, CLI + admin button)

- Prerequisites: auth; role=admin; MFA step-up; LAB_ENABLED=true; ENV != prod
- User steps: open the Defence lab tab, press Run probes
- Observable result: pass/fail per THREAT_MODEL-catalogue probe id, with history
- Key source files: `frontend/app/admin/page.tsx`
- Docs: `docs/adr/adr_phase13_dashboard.md`

## Coverage: features with no e2e spec

**23 of 39** registry entries have no covering `frontend/e2e/*.spec.ts` file today (backend-only infrastructure, CI/offline quality checks, or an Azure-only property -- not necessarily a gap, but every one below falls back to its own health/quality probe instead of a Playwright run):

- `stack-health-endpoint` -- Liveness endpoint (/health)
- `stack-postgres` -- Postgres
- `stack-migrations` -- Alembic migrations
- `stack-embeddings` -- Embeddings (Ollama bge-m3)
- `stack-reranker` -- Cross-encoder reranker (shared, quantized)
- `stack-llm-provider` -- LLM provider (Ollama / Azure OpenAI)
- `stack-prometheus` -- Prometheus metrics (in-process registry)
- `rag-abstention` -- Abstention (no reliable source)
- `rag-chitchat` -- Chit-chat (incl. typo-tolerant small talk)
- `rag-agentic-router` -- Agentic router (query-rewrite retry, CRAG-lite)
- `quality-eval-vs-baseline` -- Eval vs baseline (golden set)
- `quality-judge-kappa` -- Correctness judge (Cohen's kappa validated)
- `quality-misspelling-probe` -- Misspelling tolerance (query typo robustness)
- `identity-lockout` -- Login lockout (exponential back-off)
- `identity-password-policy-reset` -- Password policy + reset
- `abuse-rate-limiting` -- Rate limiting (token bucket)
- `abuse-signup-risk` -- Signup risk scoring (disposable domains + velocity)
- `abuse-signed-client-ip` -- Signed client IP (BFF -> backend)
- `abuse-email-abuse-limits` -- Email abuse limits (per-IP/recipient/global)
- `privacy-encrypted-conversations` -- Encrypted conversations (per-user key)
- `privacy-deletion-replay` -- Deletion replay (restore safety)
- `browser-exposed-files` -- Exposed-files check (.git/.env/source maps never served)
- `azure-internal-ingress` -- Internal ingress (backend has no public URL)

