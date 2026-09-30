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
