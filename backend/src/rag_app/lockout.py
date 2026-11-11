"""Login lockout (0007 "part 2", T12.4.2): exponential back-off keyed by
``HMAC(LOCKOUT_HMAC_SECRET, email)`` -- never by ``user_id``. Keying by the submitted email
(not the account) means a login attempt against an email that is not even registered is
still throttled exactly like a real account (F5/API-6, enumeration-safe, same reasoning as
the constant-time dummy hash in ``rag_app.security.dummy_password_hash``, T12.4.1): an
attacker who floods a never-registered address learns nothing different from flooding a
real one.

**X10 (no TOCTOU, docs/security/THREAT_MODEL.md "X10").** ``record_failed_login`` is ONE
atomic ``INSERT ... ON CONFLICT (email_hmac) DO UPDATE`` whose ``SET`` clauses compute the
new ``failed_count``/``locked_until`` from the CURRENT row in the same statement -- never a
SELECT to read the counter followed by a separate UPDATE. Under Postgres, the upsert takes
the row's lock for the duration of the statement, so two concurrent failed attempts against
the SAME key serialize: the second always sees the first's committed update, never a stale
copy of the counter. Follows the same template as
``rag_app.api.admin.claim_recovery_code`` (REC-04, written first in 12b-A).

**Exponential back-off.** The first ``LOCKOUT_THRESHOLD`` failures in a row cost nothing;
from the next one, the lock grows as ``LOCKOUT_BASE_SECONDS * 2 ** (failed_count -
LOCKOUT_THRESHOLD)``, capped at ``LOCKOUT_MAX_SECONDS``. A gap of ``LOCKOUT_RESET_AFTER_
SECONDS`` with no further failures resets the counter to zero (so a stale row for an
address nobody is actively attacking does not accumulate forever).

**DEBT-034 (design note, 12b-B).** Keying by email bounds how long a third party can keep
ONE victim locked out (the back-off resets after ``LOCKOUT_RESET_AFTER_SECONDS`` of
inactivity, and the cap is finite) -- it does NOT, by itself, stop a third party flooding
MANY different accounts' counters from the SAME shared Azure address (no signed real IP on
the public backend URL yet, 12a-C/DA-12aC-2). ``rag_app.api.deps.rate_limit_login`` adds the
other axis today (keyed by email + real/signed IP already); DEBT-034 itself closes once the
backend ingress goes internal (T12.6b.5), which removes the shared-address problem this
note exists to flag -- do not tick it here.

**Erasure (X3).** ``email_hmac`` matches the X3 scan's ``*_hmac`` pattern but carries no
``user_id`` FK (a lockout row can legitimately outlive -- or never even correspond to -- a
registered account): it is deleted in the erasure REQUEST path's short transaction while the
email is still known (``rag_app.erasure._short_step``, before the email is scrubbed), and by
the age-based retention purge below (``purge_expired_lockouts``) for rows whose email was
never registered or was already erased. See ``rag_app.erasure.X3_EXEMPTIONS``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

LOCKOUT_THRESHOLD = 5  # free attempts before any lock is applied
LOCKOUT_BASE_SECONDS = 30
LOCKOUT_MAX_SECONDS = 900  # 15 minutes
LOCKOUT_RESET_AFTER_SECONDS = 3600  # an hour of silence resets the counter to zero

# Retention (X9-style constant, but specific to this table): a row this old, AND not
# currently locked, is stale -- deleted by the purger's retention sweep regardless of
# whether its email ever became/stayed a real account.
LOCKOUT_RETENTION_DAYS = 30


def lockout_key(secret: str, email: str) -> str:
    """``HMAC-SHA256(secret, lower(email))``, hex-encoded -- never the email itself."""
    return hmac.new(secret.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class LockoutState:
    failed_count: int
    locked_until: datetime | None


# The atomic upsert (X10): every branch is a CASE inside the SAME statement, keyed on the
# CURRENT row's own `updated_at`/`failed_count` -- no SELECT anywhere in this function.
_RECORD_FAILURE_SQL = """
    INSERT INTO login_lockouts (email_hmac, failed_count, locked_until, updated_at)
    VALUES (:key, 1, NULL, now())
    ON CONFLICT (email_hmac) DO UPDATE SET
        failed_count = CASE
            WHEN login_lockouts.updated_at < now() - make_interval(secs => :reset_after)
                THEN 1
            ELSE login_lockouts.failed_count + 1
        END,
        locked_until = CASE
            WHEN login_lockouts.updated_at < now() - make_interval(secs => :reset_after)
                THEN NULL
            WHEN login_lockouts.failed_count + 1 > :threshold THEN
                now() + make_interval(secs => LEAST(
                    :max_lock,
                    :base_lock * (2.0 ^ (login_lockouts.failed_count + 1 - :threshold))
                ))
            ELSE login_lockouts.locked_until
        END,
        updated_at = now()
    RETURNING failed_count, locked_until
"""


def record_failed_login(session: Session, secret: str, email: str) -> LockoutState:
    """Atomically count one more failed attempt against ``email`` and return the resulting
    state (X10 -- see the module docstring). Caller commits."""
    key = lockout_key(secret, email)
    row = session.execute(
        text(_RECORD_FAILURE_SQL),
        {
            "key": key,
            "reset_after": LOCKOUT_RESET_AFTER_SECONDS,
            "threshold": LOCKOUT_THRESHOLD,
            "max_lock": LOCKOUT_MAX_SECONDS,
            "base_lock": LOCKOUT_BASE_SECONDS,
        },
    ).one()
    return LockoutState(failed_count=row[0], locked_until=row[1])


def locked_until(session: Session, secret: str, email: str) -> datetime | None:
    """A cheap read: is ``email`` currently locked? Not itself a TOCTOU-sensitive decision --
    nothing here is mutated based on this read; the WORST case of a race at the exact lock
    boundary is one extra login attempt let through, not an unbounded bypass."""
    key = lockout_key(secret, email)
    return session.execute(
        text("SELECT locked_until FROM login_lockouts WHERE email_hmac = :key"), {"key": key}
    ).scalar()


def clear_lockout(session: Session, secret: str, email: str) -> None:
    """A successful login clears the counter entirely (T12.4.2) -- the next failure starts
    fresh at 1, same as a row that never existed."""
    key = lockout_key(secret, email)
    session.execute(text("DELETE FROM login_lockouts WHERE email_hmac = :key"), {"key": key})


def delete_lockout_for_email(session: Session, secret: str, email: str) -> None:
    """Erasure request path (``rag_app.erasure._short_step``): deletes the row for ``email``
    while it is still known, before it is scrubbed. A thin, clearly-named alias of
    ``clear_lockout`` so the erasure call site reads as "delete for this reason", not
    "a login just succeeded"."""
    clear_lockout(session, secret, email)


# --- retention purge (not an X3 purge step: no user_id to batch by) -----------------------

_PURGE_EXPIRED_SQL = """
    WITH victim AS (
        SELECT email_hmac FROM login_lockouts
         WHERE updated_at < now() - make_interval(days => :days)
           AND (locked_until IS NULL OR locked_until < now())
         LIMIT :n FOR UPDATE SKIP LOCKED)
    DELETE FROM login_lockouts WHERE email_hmac IN (SELECT email_hmac FROM victim)
"""


def purge_expired_lockouts(
    engine: Engine, *, retention_days: int = LOCKOUT_RETENTION_DAYS, batch_size: int = 1000
) -> int:
    """Age-based retention sweep (run by the purger Job, ``secrag_purger``): deletes rows
    older than ``retention_days`` that are not currently locked -- bounds table growth from
    login attempts against emails that were never registered (or already erased, defence in
    depth alongside the erasure request path's own delete). Short transaction, own lock/
    statement timeout, same pattern as the batched purge steps in ``rag_app.purger``."""
    deleted = 0
    while True:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '2s'"))
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            batch = conn.execute(
                text(_PURGE_EXPIRED_SQL), {"days": retention_days, "n": batch_size}
            ).rowcount
        deleted += batch
        if batch < batch_size:
            break
    return deleted
