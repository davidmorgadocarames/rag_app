"""Login lockout (0007 "part 2", T12.4.2): X10 no-TOCTOU atomic counter, exponential
back-off, and the erasure interaction (the row is deleted while the email is still known).

Same REC-04 pattern (docs/security/THREAT_MODEL.md "X10"): a genuine thread-concurrency
test PLUS a deterministic structural companion, because a race condition in a counter is
often only probabilistically reproduced by a threaded test alone.
"""

from __future__ import annotations

import inspect
import threading
import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from rag_app.db.models import User
from rag_app.erasure import request_erasure
from rag_app.lockout import (
    _RECORD_FAILURE_SQL,
    LOCKOUT_MAX_SECONDS,
    LOCKOUT_RESET_AFTER_SECONDS,
    LOCKOUT_THRESHOLD,
    clear_lockout,
    locked_until,
    lockout_key,
    purge_expired_lockouts,
    record_failed_login,
)

pytestmark = [pytest.mark.db, pytest.mark.security]

SECRET = "lockout-test-secret-" + "s" * 20


def _email() -> str:
    return f"lockout-{uuid.uuid4().hex[:10]}@example.test"


# --- X10: atomic counter under genuine concurrency ------------------------------------------


def test_concurrent_failed_logins_against_the_same_key_lose_no_updates(db_engine: Engine) -> None:
    """X10: ``record_failed_login`` is one atomic ``INSERT ... ON CONFLICT DO UPDATE``, so N
    threads failing the SAME login at once are ALL counted -- a SELECT-then-UPDATE shape
    would instead let two concurrent readers both see the same ``failed_count`` and both
    write ``+1``, losing updates (the final count would be < N)."""
    email = _email()
    thread_count = 8
    barrier = threading.Barrier(thread_count)

    def fail() -> None:
        barrier.wait()
        with Session(db_engine) as session:
            record_failed_login(session, SECRET, email)
            session.commit()

    threads = [threading.Thread(target=fail) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    with Session(db_engine) as session:
        count = session.execute(
            text("SELECT failed_count FROM login_lockouts WHERE email_hmac = :k"),
            {"k": lockout_key(SECRET, email)},
        ).scalar_one()
    assert count == thread_count, "lost update: the atomic upsert regressed to SELECT-then-UPDATE"


def test_record_failed_login_is_a_single_atomic_statement() -> None:
    """Deterministic companion to the threaded race test above (same reasoning as
    ``test_claim_recovery_code_is_a_single_atomic_statement``, REC-04): the function itself
    must issue exactly one ``session.execute`` call, and the SQL it runs must be an upsert
    (``INSERT ... ON CONFLICT ... DO UPDATE``), never a SELECT to read the counter followed
    by a separate UPDATE."""
    source = inspect.getsource(record_failed_login)
    assert source.count("session.execute(") == 1, (
        "record_failed_login must issue exactly one DB statement (a single atomic upsert), "
        "not a SELECT followed by a separate UPDATE"
    )
    assert "select(" not in source and "session.query(" not in source
    sql = _RECORD_FAILURE_SQL
    assert "INSERT INTO login_lockouts" in sql
    assert "ON CONFLICT" in sql and "DO UPDATE SET" in sql


# --- exponential back-off ------------------------------------------------------------------


def test_the_first_free_attempts_never_lock(db_engine: Engine) -> None:
    email = _email()
    with Session(db_engine) as session:
        for _ in range(LOCKOUT_THRESHOLD):
            state = record_failed_login(session, SECRET, email)
            session.commit()
            assert state.locked_until is None
        assert locked_until(session, SECRET, email) is None


def test_the_next_failure_past_the_threshold_locks_and_back_off_grows(db_engine: Engine) -> None:
    email = _email()
    with Session(db_engine) as session:
        for _ in range(LOCKOUT_THRESHOLD):
            record_failed_login(session, SECRET, email)
            session.commit()
        first_lock = record_failed_login(session, SECRET, email)
        session.commit()
        assert first_lock.locked_until is not None
        second_lock = record_failed_login(session, SECRET, email)
        session.commit()
        assert second_lock.locked_until is not None
        # Exponential: the second lock (one failure further past the threshold) must end
        # strictly later than the first -- 2x the base, not a flat/fixed duration.
        assert second_lock.locked_until > first_lock.locked_until


def test_back_off_is_capped(db_engine: Engine) -> None:
    import datetime as dt

    email = _email()
    with Session(db_engine) as session:
        state = None
        for _ in range(LOCKOUT_THRESHOLD + 40):  # comfortably enough failures to hit the cap
            state = record_failed_login(session, SECRET, email)
            session.commit()
        assert state is not None and state.locked_until is not None
        remaining = (state.locked_until - dt.datetime.now(dt.UTC)).total_seconds()
        assert remaining <= LOCKOUT_MAX_SECONDS + 2  # small clock-skew tolerance


def test_a_successful_login_clears_the_counter(db_engine: Engine) -> None:
    email = _email()
    with Session(db_engine) as session:
        record_failed_login(session, SECRET, email)
        session.commit()
        clear_lockout(session, SECRET, email)
        session.commit()
        row = session.execute(
            text("SELECT 1 FROM login_lockouts WHERE email_hmac = :k"),
            {"k": lockout_key(SECRET, email)},
        ).first()
        assert row is None
        # The next failure starts fresh at 1, not continuing an old count.
        state = record_failed_login(session, SECRET, email)
        session.commit()
        assert state.failed_count == 1


def test_a_long_gap_resets_the_counter_to_zero(db_engine: Engine) -> None:
    """A row older than LOCKOUT_RESET_AFTER_SECONDS (its own stale ``updated_at``, set
    directly here rather than waiting an hour) resets to a fresh failure, not a continued
    count."""
    email = _email()
    key = lockout_key(SECRET, email)
    with Session(db_engine) as session:
        session.execute(
            text(
                "INSERT INTO login_lockouts (email_hmac, failed_count, locked_until, updated_at)"
                " VALUES (:k, 20, NULL, now() - make_interval(secs => :gap))"
            ),
            {"k": key, "gap": LOCKOUT_RESET_AFTER_SECONDS + 10},
        )
        session.commit()
        state = record_failed_login(session, SECRET, email)
        session.commit()
        assert state.failed_count == 1


# --- erasure interaction (see also test_erasure.py) ----------------------------------------


def test_locked_until_and_clear_use_the_same_key(db_engine: Engine) -> None:
    email = _email()
    with Session(db_engine) as session:
        for _ in range(LOCKOUT_THRESHOLD + 1):
            record_failed_login(session, SECRET, email)
            session.commit()
        assert locked_until(session, SECRET, email) is not None
        clear_lockout(session, SECRET, email)
        session.commit()
        assert locked_until(session, SECRET, email) is None


def test_erasure_deletes_the_login_lockouts_row_while_the_email_is_still_known(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-12bB-2: ``erasure._short_step`` deletes the ``login_lockouts`` row for the live
    email BEFORE it is scrubbed (T12.4.2, X3_EXEMPTIONS). Unlike the key-reuse check above
    (which calls ``lockout`` functions directly), this goes through a REAL registered user
    and ``erasure.request_erasure`` end to end: fail a login (creating the row), erase the
    account, and check the row for that user's ``email_hmac`` is gone."""
    monkeypatch.setenv("LOCKOUT_HMAC_SECRET", SECRET)
    email = _email()
    with Session(db_engine) as session:
        user = User(email=email, password_hash="not-a-real-hash-" + uuid.uuid4().hex)
        session.add(user)
        session.commit()
        user_id = user.id

        record_failed_login(session, SECRET, email)
        session.commit()
        key = lockout_key(SECRET, email)
        row_before = session.execute(
            text("SELECT 1 FROM login_lockouts WHERE email_hmac = :k"), {"k": key}
        ).first()
        assert row_before is not None  # sanity: the row exists before erasure

        assert request_erasure(session, user_id) is True

        row_after = session.execute(
            text("SELECT 1 FROM login_lockouts WHERE email_hmac = :k"), {"k": key}
        ).first()
        assert row_after is None


# --- retention purge -------------------------------------------------------------------------


def test_purge_expired_lockouts_removes_only_old_unlocked_rows(db_engine: Engine) -> None:
    stale_unlocked = lockout_key(SECRET, _email())
    fresh_unlocked = lockout_key(SECRET, _email())
    stale_but_locked = lockout_key(SECRET, _email())
    with Session(db_engine) as session:
        session.execute(
            text(
                "INSERT INTO login_lockouts (email_hmac, failed_count, locked_until, updated_at)"
                " VALUES (:k, 3, NULL, now() - interval '31 days')"
            ),
            {"k": stale_unlocked},
        )
        session.execute(
            text(
                "INSERT INTO login_lockouts (email_hmac, failed_count, locked_until, updated_at)"
                " VALUES (:k, 1, NULL, now())"
            ),
            {"k": fresh_unlocked},
        )
        session.execute(
            text(
                "INSERT INTO login_lockouts (email_hmac, failed_count, locked_until, updated_at)"
                " VALUES (:k, 10, now() + interval '1 hour', now() - interval '31 days')"
            ),
            {"k": stale_but_locked},
        )
        session.commit()

    purge_expired_lockouts(db_engine, retention_days=30)

    # Scoped to THIS test's own three keys -- the harness database is shared across the
    # whole module (session-scoped fixture), so a bare "every row" query would also see
    # other tests' still-live rows.
    with Session(db_engine) as session:
        remaining = set(
            session.execute(
                text("SELECT email_hmac FROM login_lockouts WHERE email_hmac = ANY(:keys)"),
                {"keys": [stale_unlocked, fresh_unlocked, stale_but_locked]},
            ).scalars()
        )
    assert remaining == {fresh_unlocked, stale_but_locked}
