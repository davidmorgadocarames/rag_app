"""Master-key fingerprint check at API start-up (T11.2.4, PHASE_PLANNING 11.2, R5-5).

The database remembers WHICH ``DATA_MASTER_KEY`` wrapped its users' data keys, as a
one-row table ``master_key_fingerprint`` (migration 0005). At every API start:

- **row present** → the running key must produce the same fingerprint, otherwise the API
  refuses to start (``MasterKeyMismatchError``). A changed or mistyped key can no longer
  start an app that answers every request with ``InvalidToken``/500 — the diagnosed class
  (b) of 11.1.
- **row absent** (first start after 0005) → **R5-5 guard**: every existing ``user_keys`` row
  must unwrap with the running key. If any does not, the API refuses to start and stores
  nothing (``UnreadableKeysError``): the unreadable accounts must be recovered
  (D-2026-09-29-2, ``scripts/azure/key_recovery.py``) or purged with a tombstone BEFORE the
  first fingerprint write, so the fingerprint never blesses a key that cannot read existing
  data. Otherwise the fingerprint is stored (``INSERT … ON CONFLICT DO NOTHING``, then read
  back and compared, so two replicas starting together agree or one refuses).

The fingerprint is ``HMAC-SHA256(key=<the 32 raw key bytes>, msg=FINGERPRINT_CONTEXT)``,
hex — a key check value that reveals nothing usable about the key. Messages name counts and
the rule only, never a key, a fingerprint or an account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet
from sqlalchemy import Connection, Engine, text

FINGERPRINT_ALGORITHM = "hmac-sha256-v1"
FINGERPRINT_CONTEXT = b"secrag data-master-key fingerprint v1"


class MasterKeyMismatchError(RuntimeError):
    """``DATA_MASTER_KEY`` differs from the key the database was initialised with."""


class UnreadableKeysError(RuntimeError):
    """Existing wrapped keys do not unwrap with ``DATA_MASTER_KEY`` (R5-5, before the first
    fingerprint write)."""


def fingerprint(master_key: str) -> str:
    """Key check value of a Fernet master key (url-safe base64 of 32 bytes)."""
    raw = base64.urlsafe_b64decode(master_key.encode())
    return hmac.new(raw, FINGERPRINT_CONTEXT, hashlib.sha256).hexdigest()


def count_unreadable_keys(conn: Connection, master_key: str) -> tuple[int, int]:
    """(total, unreadable) ``user_keys`` rows for ``master_key`` — counts only."""
    master = Fernet(master_key.encode())
    total = bad = 0
    for (blob,) in conn.execute(text("SELECT wrapped_key FROM user_keys")):
        total += 1
        try:
            master.decrypt(bytes(blob))
        except Exception:  # noqa: BLE001 - InvalidToken or a malformed blob
            bad += 1
    return total, bad


def check_master_key_fingerprint(engine: Engine, master_key: str) -> str:
    """Compare (or, the first time, store) the master-key fingerprint.

    Returns ``"match"`` or ``"stored"``; raises ``MasterKeyMismatchError`` or
    ``UnreadableKeysError`` so the API lifespan refuses to start.
    """
    mine = fingerprint(master_key)
    with engine.begin() as conn:
        stored = conn.execute(
            text("SELECT fingerprint, algorithm FROM master_key_fingerprint WHERE id = 1")
        ).one_or_none()
        if stored is None:
            total, bad = count_unreadable_keys(conn, master_key)
            if bad:
                raise UnreadableKeysError(
                    f"refusing to start: {bad} of {total} stored user keys do not unwrap with"
                    " this DATA_MASTER_KEY, and no master-key fingerprint is stored yet."
                    " Recover those accounts (scripts/azure/key_recovery.py) or purge them with"
                    " a tombstone (R5-5) BEFORE the first start writes the fingerprint; never"
                    " start with a key that cannot read the existing data."
                )
            conn.execute(
                text(
                    "INSERT INTO master_key_fingerprint (id, fingerprint, algorithm)"
                    " VALUES (1, :fp, :alg) ON CONFLICT (id) DO NOTHING"
                ),
                {"fp": mine, "alg": FINGERPRINT_ALGORITHM},
            )
            stored = conn.execute(
                text("SELECT fingerprint, algorithm FROM master_key_fingerprint WHERE id = 1")
            ).one()
            outcome = "stored"
        else:
            outcome = "match"
    fp, algorithm = stored
    if algorithm != FINGERPRINT_ALGORITHM or not hmac.compare_digest(str(fp), mine):
        raise MasterKeyMismatchError(
            "refusing to start: DATA_MASTER_KEY does not match the master-key fingerprint"
            " stored in the database — it is not the key this database's user keys were"
            " wrapped with. Restore the right key; never re-initialise the fingerprint to"
            " make a new key start (existing data would become unreadable)."
        )
    return outcome
