# ADR 0002 — GDPR data erasure (crypto-shred + hard delete + replay)

- **Status:** Accepted (2026-09-19)
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

## Backups & retention

- Backups are **"put beyond use"** (ICO): not used for any decision, access-controlled,
  and permanently deleted when the backup rotates.
- **Retention schedule (documented):** backups are kept at most **7 days** and rotate
  daily; deleted users' data therefore disappears from backups within 7 days. This is a
  justified, proportionate schedule for a small app — GDPR requires it to be documented
  and justified, not a specific legal number.
- Tombstones (`deletion_requests`) contain **no personal data** and are kept indefinitely
  as the audit/replay record.

## Transparency

Users are told, at deletion time and in the privacy notice, that: live data is erased
immediately; encrypted copies in backups are put beyond use and deleted within the 7-day
backup window; and deletions are replayed after any restore.

## Consequences

- Deleting the key is defense-in-depth, not the sole mechanism — the primary erasure is
  the hard delete, and `purge_orphaned` enforces the key↔data link.
- `replay_deletions` + `purge_orphaned` must run in the DR runbook before the system
  reopens.
