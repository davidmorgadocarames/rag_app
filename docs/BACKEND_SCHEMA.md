# Backend Schema — SecRAG

How user and corpus data is stored and organised, including the authentication flow and every table
with its columns and relationships. Target store: **PostgreSQL 16 + pgvector**.

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
| title | text | derived from first question |
| created_at | timestamptz | |

### messages
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| conversation_id | uuid (FK→conversations) | |
| role | text | `user` / `assistant` |
| content | text | encrypted at rest (user data) |
| abstained | boolean | assistant abstention flag |
| created_at | timestamptz | |

### citations  (join: message ↔ chunk)
| Column | Type | Notes |
|--------|------|-------|
| id | uuid (PK) | |
| message_id | uuid (FK→messages) | |
| chunk_id | uuid (FK→chunks) | |
| score | float | rerank/retrieval score |

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
| created_at | timestamptz | |
> Contains user content → **must be included in crypto-shred / erasure**.

## 4. Erasure coverage (GDPR)

Deleting a user removes/renders-unrecoverable, at minimum:
`users`, `user_keys`, `*_tokens`, `sessions`, `login_attempts` (for that user), `conversations`,
`messages`, `citations`, `query_logs` — **plus** any per-user cache entries and external trace records.
Corpus tables (`documents`, `chunks`) are shared and not user-owned.
