# Backend Schema — SecRAG

How user and corpus data is stored and organised, including the authentication flow and every table
with its columns and relationships. Target store: **PostgreSQL 16 + pgvector**.

Sections 1–3 are the **target design** from the planning phase (some tables — sessions,
password resets, login attempts, query logs — arrive in later phases). What the migrations
create **today** (head `0005_persistence_erasure`), including the database roles and their
grants, is in [section 5](#5-as-implemented-migrations-0001-0005).

## 1. Authentication flow

```
Sign up ─▶ create users row (email_verified=false)
        ─▶ create email_verification_tokens row
        ─▶ send email
Verify  ─▶ validate token (unexpired, unused) ─▶ users.email_verified=true ─▶ consume token
Login   ─▶ rate-limit check (login_attempts) ─▶ verify argon2 hash ─▶ require email_verified
        ─▶ issue session (sessions/refresh_tokens) + short-lived JWT
Logout  ─▶ revoke session
Reset   ─▶ password_reset_tokens (same lifecycle as verification tokens)
```

- Passwords stored as **argon2** hashes only (never plaintext).
- Per-user **encryption key** (`user_keys`) enables crypto-shredding: deleting the key makes the user's
  encrypted data unrecoverable.

## 2. Entity relationships

```
users 1───∞ email_verification_tokens
users 1───∞ password_reset_tokens
users 1───∞ sessions
users 1───∞ login_attempts        (also keyed by IP)
users 1───1 user_keys
users 1───∞ conversations 1───∞ messages ∞───∞ citations ∞───1 chunks
documents 1───∞ chunks
users 1───∞ query_logs
```

## 3. Tables

### users
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| email | citext, unique | |
| password_hash | text | argon2 |
| email_verified | boolean | default false |
| status | text | `active` / `disabled` |
| risk_score | int | signup risk (Sybil defense) |
| created_at | timestamptz | |
| updated_at | timestamptz | |

### user_keys  (crypto-shred)
| Column | Type | Notes |
|--------|------|-------|
| user_id | uuid (PK, FK→users) | |
| data_key_encrypted | bytea | per-user key, wrapped by a master key |
| created_at | timestamptz | |
> Deleting this row (and the wrapped key) renders the user's encrypted data unrecoverable.

### email_verification_tokens / password_reset_tokens
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| user_id | uuid (FK→users) | |
| token_hash | text | store hash, not the raw token |
| expires_at | timestamptz | |
| used_at | timestamptz, null | consumed once |
| created_at | timestamptz | |

### sessions
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| user_id | uuid (FK→users) | |
| refresh_token_hash | text | |
| user_agent | text | |
| ip | inet | |
| expires_at | timestamptz | |
| revoked_at | timestamptz, null | |
| created_at | timestamptz | |

### login_attempts  (rate limiting / abuse)
| Column | Type | Notes |
|--------|------|-------|
| id | bigserial (PK) | |
| email | citext, null | may be unknown |
| ip | inet | |
| success | boolean | |
| created_at | timestamptz | index on (ip, created_at) |

### documents  (corpus)
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| source | text | filename / URL |
| title | text | |
| version | text | e.g. `2021`, `2025`, `current` |
| effective_date | date | for freshness/version logic |
| valid_until | date, null | supersession |
| authority | text | source authority ranking |
| content_hash | text | dedupe / change detection |
| created_at | timestamptz | |

### chunks
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| document_id | uuid (FK→documents) | |
| heading | text | semantic boundary |
| ordinal | int | position within doc |
| text | text | chunk content |
| embedding | vector(1024) | pgvector; `bge-m3` dim |
| tsv | tsvector | for BM25/keyword (hybrid) |
| version | text | denormalised from document |
| effective_date | date | denormalised |
> Indexes: HNSW/IVFFlat on `embedding`; GIN on `tsv`.

### conversations
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| user_id | uuid (FK→users) | |
| title_encrypted | bytea, null | optional user-set title, encrypted with the per-user key; when null the title is derived on the fly from the first message |
| created_at | timestamptz | |

### messages
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| conversation_id | uuid (FK→conversations) | |
| role | text | `user` / `assistant` |
| content_encrypted | bytea | Fernet-encrypted **JSON** (per-user key). User: `{text}`. Assistant: `{text, citations, abstained, grounded}` — citations are embedded so they survive a reload without a separate table |
| prompt_tokens | int, null | assistant only; backs the "conversation total" UI |
| completion_tokens | int, null | assistant only |
| created_at | timestamptz | |
> Citations (marker, chunk_uid, heading, version, effective_date) are stored **inside** the encrypted
> assistant JSON rather than a separate `citations` table, so they are covered by crypto-shred for free.

### query_logs  (observability / eval)
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| user_id | uuid (FK→users, null) | |
| route | text | router decision |
| original_query | text | |
| rewritten_query | text, null | |
| retrieved_chunk_ids | uuid[] | with scores |
| grader_decision | text | |
| groundedness | float | |
| latency_ms | int | |
| token_cost | int | for cost-aware limits |
| llm_provider | text, null | *optional (Phase 10)*: which model tier served this answer — e.g. `ollama` / `azure_openai`; denormalised so cost/quality can be compared per provider |
| created_at | timestamptz | |
> Contains user content → **must be included in crypto-shred / erasure**.
> The `llm_provider` value is non-PII, so it needs no special erasure handling.

## 4. Erasure coverage (GDPR)

**As implemented (11a, asynchronous erasure — ADR phase 11 decision 6).** `DELETE /account`
runs one short transaction: delete `user_keys` (crypto-shred), set `users.email` and
`users.password_hash` to NULL, set `users.deleted_at`, and upsert `deletion_requests`
(`status = pending`). The purger (`rag_app.purger`, role `secrag_purger`) follows the
purge-step registry `rag_app.erasure.PURGE_STEPS`, leaf-first — `messages` → `conversations`
→ `email_verification_tokens` → `users` — in batches with `FOR UPDATE SKIP LOCKED`, recording
`progress`/`attempts`/`last_error` on the tombstone and one row per run in `purger_runs`.
**Schema-scan rule (X3):** every column named `user_id`, `*_user_id`, `admin_id` or `*_hmac`
needs a purge step or a documented exemption (today: `deletion_requests.user_id` — the opaque
tombstone kept for replay; `user_keys.user_id` — deleted in the request transaction); a test
fails otherwise. Tables added later (sessions, lockout, audit, …) add their step with their
migration.

Migration 0005 columns used here: `users.deleted_at`, nullable `users.email`/`password_hash`,
`deletion_requests.status` (`pending`/`running`/`done`/`failed`), `progress` (jsonb, rows
deleted per step), `attempts`, `last_error` (error class only), `updated_at`,
`completed_at`; `purger_runs` (`started_at`, `finished_at`, `requests_processed`, `errors`,
`last_error`). Tombstones stay in the database — a minimal record that an erased account
existed (random id, dates, status, counts; no email, no content), disclosed in the privacy
text; deleting `done` tombstones older than ~60 days is planned with the `retention` role.
The exports leave out those `done` for more than 15 days.

**Target list (planning):**

Deleting a user removes/renders-unrecoverable, at minimum:
`users`, `user_keys`, `*_tokens`, `sessions`, `login_attempts` (for that user), `conversations`,
`messages`, `citations`, `query_logs` — **plus** any per-user cache entries and external trace records.
Corpus tables (`documents`, `chunks`) are shared and not user-owned.

## 5. As implemented (migrations 0001-0005)

Source of truth: `backend/src/rag_app/db/models.py` and `backend/migrations/versions/`. Head:
**`0005_persistence_erasure`** (Phase 11a, expand-only — see the
[Definition of Done](DEFINITION_OF_DONE.md#phase-specific-norms), expand/contract).

| Table | Columns (type; nullable = null) | Notes |
|---|---|---|
| `users` | `id` uuid PK · `email` varchar unique, **null since 0005** · `password_hash` varchar, **null since 0005** · `email_verified` bool · `created_at` · **`deleted_at`** timestamptz null (0005) | an erased account has `email`/`password_hash` NULL and `deleted_at` set until the purger removes the row; login and token checks ignore it |
| `user_keys` | `user_id` uuid PK → `users` (cascade) · `wrapped_key` bytea · `created_at` | the per-user Fernet key wrapped by `DATA_MASTER_KEY`; deleted in the erasure request (crypto-shred) |
| `conversations` | `id` · `user_id` → `users` (cascade) · `title_encrypted` bytea null · `created_at` | |
| `messages` | `id` · `conversation_id` → `conversations` (cascade) · `role` · `content_encrypted` bytea · `prompt_tokens`, `completion_tokens` int null · `created_at` | citations inside the encrypted JSON; failed/interrupted turns are stored as assistant error markers |
| `email_verification_tokens` | `id` · `user_id` → `users` (cascade) · `token_hash` unique · `expires_at` · `used_at` null · `created_at` | only the hash is stored |
| `deletion_requests` (tombstones) | `id` · `user_id` uuid **unique, no FK** · `requested_at` · **0005:** `status` (`pending`/`running`/`done`/`failed`, default `pending`; rows from before 0005 = `done`) · `progress` jsonb (rows deleted per purge step) · `attempts` int · `last_error` text null (error class only) · `updated_at` · `completed_at` null | partial index `ix_deletion_requests_open` on `requested_at` where the status is open; no personal data; exported to Blob `tombstones/` (local `.tombstones/`) for restores |
| `purger_runs` (0005) | `id` uuid PK (`gen_random_uuid()`) · `started_at` · `finished_at` null · `requests_processed` · `errors` · `last_error` null | one row per purger run; no personal data |
| `usage_daily` (0005) | `day` date PK · `answers` int ≥ 0 · `tokens` bigint ≥ 0 | global daily answer cap (R6-1); no user id |
| `master_key_fingerprint` (0005) | `id` smallint PK, `CHECK (id = 1)` · `fingerprint` · `algorithm` · `created_at` | HMAC-SHA256 of `DATA_MASTER_KEY`; the API refuses to start on a mismatch, and writes the first row only when every stored user key unwraps (R5-5) |
| `documents` | `id` · `slug` unique · `source`, `title` null · `version` · `effective_date` null · `category_rank` null · `created_at` | corpus, shared |
| `chunks` | `id` · `document_id` → `documents` (cascade) · `chunk_uid` unique · `heading` · `ordinal` · `text` · `embedding` vector(1024) · `version` · `effective_date` null | corpus, shared |

**Roles and grants.** The application and the migrations run as the database **owner**
(a restricted app role is planned for a later phase). `db/roles.sql` (idempotent, run as
the owner before every migrate) creates two `NOLOGIN` roles; LOGIN and passwords are set
outside Alembic by `scripts/db/apply_roles.sh` (it sends only a client-side SCRAM verifier):

| Role | Grants | Used by |
|---|---|---|
| `secrag_backup` | `CONNECT`, `USAGE` on `public`; `SELECT` on every table and sequence, and **default privileges** so every future table is readable too (0005 also grants its new tables explicitly) | `scripts/db/backup.sh` (`pg_dump` → `age`), the backup Job |
| `secrag_purger` | `CONNECT`, `USAGE`; `messages`, `conversations`, `email_verification_tokens`: `SELECT, DELETE, UPDATE (id)` (row locks with `FOR UPDATE SKIP LOCKED`, never a content change) · `users`: `SELECT, UPDATE, DELETE` · `deletion_requests`: `SELECT, UPDATE` · `user_keys`: `SELECT` · `purger_runs`: `SELECT, INSERT, UPDATE` | `rag_app.purger` (purge Job, compose `purger`) |

0005 checks that both roles exist (a missing role fails the upgrade with a clear message)
and its downgrade revokes the purger's grants. The **downgrade refuses** while any user has
`email`/`password_hash` NULL or `deleted_at` set: it is never a rollback path (roll images
back instead — [ADR phase 11, Rollback](adr/adr_phase11_stability.md#rollback)).
