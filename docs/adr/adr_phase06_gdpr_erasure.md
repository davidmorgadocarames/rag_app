# ADR phase 6 — GDPR data erasure (crypto-shred + hard delete + replay)

- **Status:** Accepted (2026-09-19); **refined in Phase 11a** — "immediate hard delete"
  became "immediate crypto-shred + PII scrub, batched physical deletion ≤ 24 h, backups
  ≤ 14 days" (see *Refinement (Phase 11a)* below and
  [ADR phase 11](adr_phase11_stability.md), decision 6).
- **Context:** Phase 6b. When a user leaves, their personal data must be erased from
  production and, in a justified/limited way, from backups — and the erasure must
  survive a disaster-recovery restore. Crypto-shredding alone (delete the key, keep the
  ciphertext) is not enough: retained ciphertext is still retained data.

## Decision

Erasure is **hard delete + crypto-shred + a retained tombstone**, with a "no key → purge"
invariant.

1. **Per-user data key.** User-generated content (conversation messages) is encrypted
   at rest with a per-user key. The per-user key is stored wrapped by a master key in
   `user_keys`.
2. **On erasure (`erase_user`)**, in one transaction:
   - hard-delete all user-owned rows (conversations, messages, sessions, tokens, the
     user row);
   - delete the `user_keys` row (crypto-shred — any ciphertext that survives anywhere,
     e.g. in a backup, becomes unrecoverable);
   - insert a **tombstone** in `deletion_requests` (`user_id` + `requested_at` only — no
     PII). Tombstones are retained so deletions can be replayed.
3. **No key → purge invariant (`purge_orphaned`).** Any user content whose owner has no
   `user_keys` row is deleted. This makes "the key is gone" mechanically imply "the data
   is gone", even for data reintroduced by a restore.
4. **Disaster recovery (`replay_deletions`).** After restoring a backup and **before
   reopening**, replay every tombstone (re-erase those users) and run `purge_orphaned`,
   then verify that non-deleted users are intact.

## Refinement (Phase 11a): asynchronous erasure

The one-transaction cascade above holds locks, WAL and a pooled connection for the whole
history of the user (seconds for a large one), and `purge_orphaned`/`replay_deletions`
deleted in bulk the same way. Since 11a (D-ER):

1. **Request path — one short transaction** (`lock_timeout` 2 s, `statement_timeout` 5 s):
   delete the `user_keys` row (crypto-shred: the data is unreadable at once), scrub `email` and
   `password_hash`, set `users.deleted_at` (login and every token check ignore the account
   immediately), tombstone `pending` → **202 Accepted**: "Account deleted. Your data is
   unreadable from now on; remaining encrypted rows are removed within 24 h and backup copies
   within 14 days." Its cost does not depend on the size of the history.
2. **Purger** (`python -m rag_app.erasure purge`, role `secrag_purger`, hourly Job on Azure,
   compose loop locally): a purge-step registry, leaf-first (messages → conversations →
   verification tokens → the `users` row last); batches of ~1,000 rows, each its own short
   transaction with `FOR UPDATE SKIP LOCKED`; back-off on a lock or statement timeout;
   progress/attempts/last error on the tombstone (resumable after a kill); an advisory lock
   so runs never overlap; one `purger_runs` row per run; the tombstone export on every run.
3. **Restore**: `replay_deletions` re-applies step 1 to every tombstoned account that came
   back and re-queues it `pending`; `purge_orphaned` **enqueues** users without a key (no bulk
   delete); `restore.sh` then runs the purger before the app reopens.
4. The synchronous `erase_user` remains only for the offline key-recovery tool (no concurrent
   traffic).

The "no key → purge" invariant, the tombstones and the replay are unchanged; what changed is
*when* the rows disappear (≤ 24 h instead of inside the request) — the data is unreadable
from the first moment either way.

## Backups & retention

- Backups are **"put beyond use"** (ICO): not used for any decision, access-controlled,
  and permanently deleted when the backup rotates.
- **Retention schedule (documented):** backups are kept at most **14 days** and rotate
  daily; deleted users' data therefore disappears from backups within 14 days. This is a
  justified, proportionate schedule for a small app — GDPR requires it to be documented
  and justified, not a specific legal number. *(Amended in Phase 11a: originally 7 days;
  now the single constant X9 = 14 days in `backend/src/rag_app/retention.py`, which covers
  point-in-time restore, the encrypted Blob dumps and the local copies — see
  [ADR phase 11](adr_phase11_stability.md).)*
- Tombstones (`deletion_requests`) contain **no personal data** and are kept indefinitely
  as the audit/replay record.

## Transparency

Users are told, at deletion time and in the privacy notice, that: their data is unreadable
immediately (crypto-shred) and the remaining encrypted rows are deleted within 24 hours
(11a); encrypted copies in backups are put beyond use and deleted within the 14-day backup
window; and deletions are replayed after any restore. The privacy text also states that
operational logs are kept 30 days without content and that Azure OpenAI may retain prompts
for abuse monitoring for up to 30 days (Azure deployment).

## Consequences

- Deleting the key is defense-in-depth, not the sole mechanism — the primary erasure is
  the hard delete, and `purge_orphaned` enforces the key↔data link.
- `replay_deletions` + `purge_orphaned` must run in the DR runbook before the system
  reopens.
