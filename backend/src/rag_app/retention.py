"""Retention promise for backup copies — the single constant (X9, PHASE_TASKS cross-cutting).

Every copy of the database outside the live server is kept at most this many days: local
encrypted dumps (``scripts/db/backup.sh`` file mode), the Blob dumps (the backup Job's
oldest-blob check; the Storage lifecycle rule deletes ``backups/`` after 12 days, below the
promise), the local copies pulled from Blob (``scripts/db/backup-pull.sh``) and Azure
PostgreSQL point-in-time restore (PITR ≤ 14 days, runbook).

The shell scripts read the value from THIS file (``BACKUP_RETENTION_DAYS = <n>`` on its own
line — keep that exact form); the user-facing text, the docs and a test use it too
(``backend/tests/test_backups.py``). Change it here and nowhere else.
"""

from __future__ import annotations

BACKUP_RETENTION_DAYS = 14

# Tombstone exports (R4-3 + R5-2; DA-F-8, user decision 2026-09-30). Every export is the FULL
# list, so only the newest one is needed for a restore; older ones are removed after this many
# days (the purger prunes Blob `tombstones/` and its local folder; backup-pull.sh prunes the
# local copies), and the newest valid export is always kept. Longer than the backup retention,
# so every backup still restorable has an export at least as new as itself.
TOMBSTONE_EXPORT_RETENTION_DAYS = 30

# A tombstone `done` for more than this many days is left out of the exports (PHASE_PLANNING
# 11.2b): no backup copy is older than BACKUP_RETENTION_DAYS, so none can revive that account.
# The row itself stays in the database.
TOMBSTONE_EXPORT_DONE_DAYS = BACKUP_RETENTION_DAYS + 1
