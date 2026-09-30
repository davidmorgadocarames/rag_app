"""Asynchronous erasure (11.2b, D-ER): purge-step registry + X3 scan (T11.2b.1), the request
path (T11.2b.2), deleted users locked out (T11.2b.3), the batched purger as ``secrag_purger``
(T11.2b.4), ``purge_orphaned``/``replay_deletions`` enqueue (T11.2b.5).

Unit tests (no database) for the registry and the 202 message; everything else runs on the
harness database (marker ``db``). The purger runs **as ``secrag_purger``** through ``SET ROLE``
on every connection (the role's privileges exactly — grants from 0005), so a missing grant
fails here. The scale test (100,000 messages, concurrent load) is the gate step
``erasure-scale`` (``rag_app.devtools.erasure_scale``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from rag_app import erasure
from rag_app.retention import BACKUP_RETENTION_DAYS

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# --- registry (no database) -------------------------------------------------------------


def test_the_registry_is_leaf_first_with_users_last() -> None:
    names = [step.name for step in erasure.PURGE_STEPS]
    assert names == ["messages", "conversations", "email_verification_tokens", "users"]
    for step in erasure.PURGE_STEPS:
        assert "SKIP LOCKED" in step.delete_batch and ":uid" in step.delete_batch
    exempt = {("deletion_requests", "user_id"), ("user_keys", "user_id")}
    assert set(erasure.X3_EXEMPTIONS) == exempt


def test_the_202_message_uses_the_retention_constant() -> None:
    from rag_app.retention import TOMBSTONE_EXPORT_RETENTION_DAYS

    message = erasure.ERASURE_ACCEPTED_MESSAGE
    assert f"within {BACKUP_RETENTION_DAYS} days" in message
    assert f"within {erasure.PURGE_DEADLINE_HOURS} h" in message
    # DA-G1-9 (i): "unreadable at once" only for the live service; the backups still hold
    # the wrapped key and the email. (ii): the kept record and its export retention.
    assert message.startswith("Account deleted. Your data in the live service is unreadable")
    assert "wrapped key" in message and "email address" in message
    assert "random id, dates and a status" in message
    assert f"kept for {TOMBSTONE_EXPORT_RETENTION_DAYS} days" in message


@pytest.mark.parametrize(
    "doc", ["docs/PRD.md", "docs/adr/adr_phase06_gdpr_erasure.md", "frontend/app/account/page.tsx"]
)
def test_the_privacy_texts_disclose_backups_and_the_kept_record(doc: str) -> None:
    body = " ".join((Path(__file__).resolve().parents[2] / doc).read_text().split())
    assert "live service" in body  # DA-G1-9 (i)
    assert "wrapped key" in body and "email address" in body
    assert "random id" in body and "30 days" in body  # DA-G1-9 (ii)


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture()
def master_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """A THROWAWAY master key + JWT secret for the app code under test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATA_MASTER_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    return key


@pytest.fixture()
def purger_engine(db_url: URL) -> Iterator[Engine]:
    """Connections that run as secrag_purger (SET ROLE on connect)."""
    engine = create_engine(db_url, future=True)

    @event.listens_for(engine, "connect")
    def _as_purger(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        with dbapi_conn.cursor() as cur:
            cur.execute("SET ROLE secrag_purger")
        dbapi_conn.commit()

    try:
        yield engine
    finally:
        engine.dispose()


def _make_user(
    engine: Engine, *, conversations: int = 2, messages: int = 5, token: bool = True
) -> tuple[uuid.UUID, str]:
    """A user with a wrapped key, conversations with encrypted messages and a verification
    token; returns (id, email)."""
    from rag_app.crypto import encrypt, generate_user_key, wrap_key
    from rag_app.db.models import (
        Conversation,
        EmailVerificationToken,
        Message,
        User,
        UserKey,
    )
    from rag_app.security import hash_password

    email = f"erasure-{uuid.uuid4().hex[:10]}@example.test"
    with Session(engine) as session:
        user = User(email=email, password_hash=hash_password("correct horse battery"))
        session.add(user)
        session.flush()
        data_key = generate_user_key()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(data_key)))
        for _ in range(conversations):
            conv = Conversation(user_id=user.id)
            session.add(conv)
            session.flush()
            for i in range(messages):
                session.add(
                    Message(
                        conversation_id=conv.id,
                        role="user",
                        content_encrypted=encrypt(data_key, json.dumps({"text": f"m{i}"})),
                    )
                )
        if token:
            session.add(
                EmailVerificationToken(
                    user_id=user.id,
                    token_hash=uuid.uuid4().hex,
                    expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                )
            )
        session.commit()
        return user.id, email


def _counts(engine: Engine, user_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM users WHERE id = :u),"
                " (SELECT count(*) FROM user_keys WHERE user_id = :u),"
                " (SELECT count(*) FROM conversations WHERE user_id = :u),"
                " (SELECT count(*) FROM messages m JOIN conversations c"
                "   ON c.id = m.conversation_id WHERE c.user_id = :u),"
                " (SELECT count(*) FROM email_verification_tokens WHERE user_id = :u),"
                " (SELECT status FROM deletion_requests WHERE user_id = :u),"
                " (SELECT id FROM deletion_requests WHERE user_id = :u)"
            ),
            {"u": user_id},
        ).one()
    keys = ("users", "keys", "conversations", "messages", "tokens", "tombstone", "request_id")
    return dict(zip(keys, row, strict=True))


def _request(engine: Engine, user_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as session:
        assert erasure.request_erasure(session, user_id)
    request_id = _counts(engine, user_id)["request_id"]
    assert isinstance(request_id, uuid.UUID)
    return request_id


def _no_sleep(_seconds: float) -> None:
    return None


# --- T11.2b.1: X3 schema scan -----------------------------------------------------------


@pytest.mark.db
def test_every_user_linked_column_has_a_purge_step_or_an_exemption(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        assert erasure.uncovered_user_columns(session) == []


@pytest.mark.db
def test_an_unregistered_user_linked_table_fails_the_scan(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("CREATE TABLE x3_probe (id int, owner_user_id uuid, email_hmac text)"))
    try:
        with Session(db_engine) as session:
            assert erasure.uncovered_user_columns(session) == [
                "x3_probe.email_hmac",
                "x3_probe.owner_user_id",
            ]
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("DROP TABLE x3_probe"))


# --- T11.2b.2 + T11.2b.3: request path via the API ----------------------------------------


@pytest.mark.db
def test_delete_account_is_a_short_step_then_202_and_the_token_dies(
    db_engine: Engine, master_key: str
) -> None:
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.security import create_token

    user_id, email = _make_user(db_engine)
    token = create_token(str(user_id))
    client = TestClient(create_app())  # no lifespan: the fingerprint is not under test here
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=auth).status_code == 200

    res = client.delete("/account", headers=auth)
    assert res.status_code == 202
    assert res.json() == {"detail": erasure.ERASURE_ACCEPTED_MESSAGE}
    assert f"{BACKUP_RETENTION_DAYS} days" in res.json()["detail"]

    after = _counts(db_engine, user_id)
    # key gone (content undecryptable), rows still there for the purger, tombstone pending
    assert after["keys"] == 0 and after["tombstone"] == "pending"
    assert after["users"] == 1 and after["messages"] == 10 and after["conversations"] == 2
    with db_engine.connect() as conn:
        email_now, hash_now, deleted_at = conn.execute(
            text("SELECT email, password_hash, deleted_at FROM users WHERE id = :u"),
            {"u": user_id},
        ).one()
    assert email_now is None and hash_now is None and deleted_at is not None

    # T11.2b.3: the token issued before the erasure → 401 right after; login fails
    assert client.get("/auth/me", headers=auth).status_code == 401
    assert client.get("/conversations", headers=auth).status_code == 401
    login = client.post("/auth/login", json={"email": email, "password": "correct horse battery"})
    assert login.status_code == 401
    # the email is free again at once
    with Session(db_engine) as session:
        assert erasure.request_erasure(session, uuid.uuid4()) is False  # unknown user: no-op


@pytest.mark.db
def test_the_request_path_never_waits_long_for_a_lock(db_engine: Engine, master_key: str) -> None:
    user_id, _ = _make_user(db_engine, conversations=0, token=False)
    with db_engine.connect() as holder:
        holder.begin()
        holder.execute(text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": user_id})
        started = dt.datetime.now(dt.UTC)
        with Session(db_engine) as session, pytest.raises(OperationalError) as err:
            erasure.request_erasure(session, user_id)
        waited = (dt.datetime.now(dt.UTC) - started).total_seconds()
        holder.rollback()
    assert "lock timeout" in str(err.value).lower() and waited < 4
    assert _counts(db_engine, user_id)["keys"] == 1  # rolled back: nothing half-done


@pytest.mark.db
def test_a_lock_timeout_on_delete_account_is_a_retryable_503(
    db_engine: Engine, master_key: str
) -> None:
    """DA-G1-4: the API maps the request path's lock timeout to 503 + Retry-After (not a
    generic 500); nothing changed, and the same request succeeds once the lock is gone."""
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.security import create_token

    user_id, _ = _make_user(db_engine, conversations=1, messages=1)
    auth = {"Authorization": f"Bearer {create_token(str(user_id))}"}
    client = TestClient(create_app(), raise_server_exceptions=False)
    with db_engine.connect() as holder:
        holder.begin()
        holder.execute(text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": user_id})
        busy = client.delete("/account", headers=auth)
        holder.rollback()
    assert busy.status_code == 503, busy.text
    assert busy.headers["Retry-After"] == str(erasure.ERASURE_RETRY_AFTER_SECONDS)
    assert "try again" in busy.json()["detail"]
    state = _counts(db_engine, user_id)
    assert state["keys"] == 1 and state["tombstone"] is None  # nothing changed
    assert client.get("/auth/me", headers=auth).status_code == 200  # still signed in
    assert client.delete("/account", headers=auth).status_code == 202


@pytest.mark.db
def test_verify_ignores_an_erased_account(db_engine: Engine, master_key: str) -> None:
    """DA-G1-5: a verification link of an erased account (its token row lives until the
    purge) is refused and changes nothing."""
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.db.models import EmailVerificationToken
    from rag_app.security import generate_verification_token, hash_token

    user_id, _ = _make_user(db_engine, conversations=0, token=False)
    raw = generate_verification_token()
    with Session(db_engine) as session:
        session.add(
            EmailVerificationToken(
                user_id=user_id,
                token_hash=hash_token(raw),
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
            )
        )
        session.commit()
    _request(db_engine, user_id)
    client = TestClient(create_app())
    res = client.get("/auth/verify", params={"token": raw})
    assert res.status_code == 400 and res.json()["detail"] == "invalid token"
    with db_engine.connect() as conn:
        verified, used = conn.execute(
            text(
                "SELECT u.email_verified, t.used_at FROM users u"
                " JOIN email_verification_tokens t ON t.user_id = u.id WHERE u.id = :u"
            ),
            {"u": user_id},
        ).one()
    assert verified is False and used is None  # no state change on the erased account


# --- T11.2b.4: the batched purger as secrag_purger ---------------------------------------


@pytest.mark.db
def test_the_purger_removes_everything_leaf_first_in_batches(
    db_engine: Engine, purger_engine: Engine, master_key: str
) -> None:
    from rag_app.purger import Purger

    user_id, _ = _make_user(db_engine, conversations=3, messages=25)
    other_id, _ = _make_user(db_engine, conversations=1, messages=3)
    request_id = _request(db_engine, user_id)
    seen: list[str] = []
    result = Purger(
        purger_engine, batch_size=10, sleep=_no_sleep, after_batch=lambda s, n: seen.append(s)
    ).run(only=request_id)
    assert result.ok and result.done == 1
    assert result.rows == {
        "messages": 75,
        "conversations": 3,
        "email_verification_tokens": 1,
        "users": 1,
    }
    assert result.batches >= 8 + 1 + 1 + 1 and result.max_transaction_seconds < 2
    order = [s for i, s in enumerate(seen) if i == 0 or seen[i - 1] != s]
    assert order == ["messages", "conversations", "email_verification_tokens", "users"]
    gone = _counts(db_engine, user_id)
    assert gone["users"] == gone["conversations"] == gone["messages"] == gone["tokens"] == 0
    assert gone["tombstone"] == "done"
    with db_engine.connect() as conn:
        progress, completed = conn.execute(
            text("SELECT progress, completed_at FROM deletion_requests WHERE id = :i"),
            {"i": request_id},
        ).one()
        run = conn.execute(
            text(
                "SELECT finished_at IS NOT NULL, requests_processed, errors FROM purger_runs"
                " ORDER BY started_at DESC LIMIT 1"
            )
        ).one()
    assert progress == {"messages": 75, "conversations": 3, "email_verification_tokens": 1,
                        "users": 1}  # fmt: skip
    assert completed is not None and tuple(run) == (True, 1, 0)
    other = _counts(db_engine, other_id)  # another account is untouched
    assert other["users"] == 1 and other["messages"] == 3 and other["keys"] == 1


class _Killed(Exception):
    pass


@pytest.mark.db
def test_a_killed_run_is_completed_by_the_next_one(
    db_engine: Engine, purger_engine: Engine, master_key: str
) -> None:
    from rag_app.purger import Purger

    user_id, _ = _make_user(db_engine, conversations=2, messages=30)
    request_id = _request(db_engine, user_id)
    batches = 0

    def kill_after_two(_step: str, _n: int) -> None:
        nonlocal batches
        batches += 1
        if batches == 2:
            raise _Killed

    with pytest.raises(_Killed):
        Purger(purger_engine, batch_size=10, sleep=_no_sleep, after_batch=kill_after_two).run(
            only=request_id
        )
    half = _counts(db_engine, user_id)
    assert half["tombstone"] == "running" and half["messages"] == 40  # 2 batches of 10 done
    result = Purger(purger_engine, batch_size=10, sleep=_no_sleep).run(only=request_id)
    assert result.done == 1 and _counts(db_engine, user_id)["users"] == 0
    with db_engine.connect() as conn:
        attempts, progress = conn.execute(
            text("SELECT attempts, progress FROM deletion_requests WHERE id = :i"),
            {"i": request_id},
        ).one()
    assert attempts == 2 and progress["messages"] == 60


@pytest.mark.db
def test_runs_never_overlap(db_engine: Engine, purger_engine: Engine, master_key: str) -> None:
    from rag_app.purger import ADVISORY_LOCK_KEY, Purger

    user_id, _ = _make_user(db_engine, conversations=1, messages=2)
    request_id = _request(db_engine, user_id)
    with purger_engine.connect() as other_run:
        assert other_run.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
        ).scalar()
        result = Purger(purger_engine, sleep=_no_sleep).run(only=request_id)
        assert result.skipped and "skipped" in result.summary()
        assert _counts(db_engine, user_id)["tombstone"] == "pending"
        other_run.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
    assert Purger(purger_engine, sleep=_no_sleep).run(only=request_id).done == 1


@pytest.mark.db
def test_locked_rows_back_off_then_fail_and_retry_later(
    db_engine: Engine, purger_engine: Engine, master_key: str
) -> None:
    from rag_app.purger import BACKOFF_CAP_SECONDS, MAX_RETRIES, Purger

    user_id, _ = _make_user(db_engine, conversations=1, messages=3)
    request_id = _request(db_engine, user_id)
    sleeps: list[float] = []
    with db_engine.connect() as holder:
        holder.begin()  # a transaction holds one of the user's messages
        holder.execute(
            text(
                "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id"
                " WHERE c.user_id = :u LIMIT 1 FOR UPDATE OF m"
            ),
            {"u": user_id},
        )
        result = Purger(purger_engine, sleep=sleeps.append).run(only=request_id)
        holder.rollback()
    assert result.failed == 1 and not result.ok
    assert len(sleeps) == MAX_RETRIES and sleeps == sorted(sleeps)  # growing back-off
    assert max(sleeps) <= BACKOFF_CAP_SECONDS and min(sleeps) > 0  # never a tight loop
    state = _counts(db_engine, user_id)
    assert state["tombstone"] == "failed" and state["messages"] == 1  # the others went
    with db_engine.connect() as conn:
        last_error = conn.execute(
            text("SELECT last_error FROM deletion_requests WHERE id = :i"), {"i": request_id}
        ).scalar()
    assert last_error == "messages: rows locked by another transaction"
    assert Purger(purger_engine, sleep=_no_sleep).run(only=request_id).done == 1  # retried


@pytest.mark.db
def test_the_purger_never_removes_an_account_that_is_not_marked_deleted(
    db_engine: Engine, purger_engine: Engine, master_key: str
) -> None:
    from rag_app.db.models import DeletionRequest
    from rag_app.purger import Purger

    user_id, _ = _make_user(db_engine, conversations=1, messages=2)
    with Session(db_engine) as session:
        tombstone = DeletionRequest(user_id=user_id, status="pending")
        session.add(tombstone)
        session.commit()
        request_id = tombstone.id
    try:
        result = Purger(purger_engine, sleep=_no_sleep).run(only=request_id)
        assert result.failed == 1
        state = _counts(db_engine, user_id)
        assert state["users"] == 1 and state["keys"] == 1 and state["messages"] == 2
        assert state["tombstone"] == "failed"
    finally:
        with db_engine.begin() as conn:  # the shared harness DB: leave no open tombstone
            conn.execute(text("DELETE FROM deletion_requests WHERE id = :i"), {"i": request_id})


@pytest.mark.db
def test_the_export_is_written_every_run_and_old_done_entries_are_left_out(
    db_engine: Engine, purger_engine: Engine, master_key: str, tmp_path: Path
) -> None:
    from rag_app.db.models import DeletionRequest
    from rag_app.purger import LocalExport, Purger
    from rag_app.retention import TOMBSTONE_EXPORT_DONE_DAYS
    from rag_app.tombstones import read_exports

    now = dt.datetime.now(dt.UTC)
    old_done, recent_done = uuid.uuid4(), uuid.uuid4()
    with Session(db_engine) as session:
        for uid, days in ((old_done, TOMBSTONE_EXPORT_DONE_DAYS + 1), (recent_done, 3)):
            when = now - dt.timedelta(days=days)
            session.add(
                DeletionRequest(user_id=uid, status="done", requested_at=when, completed_at=when)
            )
        session.commit()
    user_id, _ = _make_user(db_engine, conversations=1, messages=1)
    request_id = _request(db_engine, user_id)
    exports = tmp_path / "exports"
    exports.mkdir(mode=0o700)
    stale = exports / f"tombstones-{now - dt.timedelta(days=40):%Y%m%dT%H%M%SZ}.jsonl"
    stale.write_text("")
    result = Purger(purger_engine, sleep=_no_sleep, export=LocalExport(exports)).run(
        only=request_id
    )
    assert result.ok and result.export and stale.name in result.pruned
    found, files = read_exports(exports)
    assert files == 1 and user_id in found and recent_done in found and old_done not in found


@pytest.mark.db
def test_the_blob_export_uploads_and_prunes(
    db_engine: Engine, purger_engine: Engine, master_key: str
) -> None:
    from rag_app.purger import BlobExport, Purger
    from test_backups import _MemoryContainer

    client = _MemoryContainer()
    now = dt.datetime.now(dt.UTC)
    old = f"tombstones/tombstones-{now - dt.timedelta(days=31):%Y%m%dT%H%M%SZ}.jsonl"
    client.blobs[old] = (b"", now)
    user_id, _ = _make_user(db_engine, conversations=1, messages=1)
    request_id = _request(db_engine, user_id)
    result = Purger(purger_engine, sleep=_no_sleep, export=BlobExport(client)).run(only=request_id)
    assert result.ok and result.pruned == [old]
    names = [n for n in client.blobs if n.startswith("tombstones/")]
    assert len(names) == 1 and str(user_id).encode() in client.blobs[names[0]][0]
    client.blobs["tombstones/tombstones-20990101T000000Z.jsonl"] = (b"", now)  # DA-F2-1
    time.sleep(1.1)  # a new export name (one per second; an existing blob is never overwritten)
    again = Purger(purger_engine, sleep=_no_sleep, export=BlobExport(client)).run(only=request_id)
    assert again.problems and not again.ok and again.pruned == []


@pytest.mark.db
def test_the_cli_prints_counts_only(
    db_engine: Engine, master_key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rag_app.purger import main

    user_id, _ = _make_user(db_engine, conversations=1, messages=2)
    request_id = _request(db_engine, user_id)
    assert main(["purge-now", str(request_id), "--export-dir", str(tmp_path / "e")]) == 0
    out = capsys.readouterr().out
    assert "1 done" in out and "messages 2" in out
    assert not UUID_RE.search(out.replace(str(tmp_path), "")) and "@" not in out


def _age(engine: Engine, request_id: uuid.UUID, hours: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE deletion_requests SET requested_at = now() - make_interval(hours => :h)"
                " WHERE id = :i"
            ),
            {"h": hours, "i": request_id},
        )


@pytest.mark.db
def test_an_overdue_erasure_is_a_warning_and_exit_1(
    db_engine: Engine,
    purger_engine: Engine,
    master_key: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DA-G1-1: a tombstone still open more than PURGE_DEADLINE_HOURS after the request
    (the 202 promise) fails the run visibly — also a run that could not do the work (time
    limit) and a run skipped because another one holds the lock."""
    from rag_app.purger import ADVISORY_LOCK_KEY, Purger, main

    user_id, _ = _make_user(db_engine, conversations=1, messages=2)
    request_id = _request(db_engine, user_id)
    try:
        fresh = Purger(purger_engine, sleep=_no_sleep, max_seconds=0).run()
        assert fresh.overdue == 0 and fresh.open_left and fresh.ok  # < 24 h: not overdue
        _age(db_engine, request_id, erasure.PURGE_DEADLINE_HOURS + 1)
        late = Purger(purger_engine, sleep=_no_sleep, max_seconds=0).run()
        assert late.overdue == 1 and not late.ok
        assert main(["purge", "--no-export", "--max-seconds", "0"]) == 1
        err = capsys.readouterr().err
        assert "purger: WARNING: 1 erasure(s) overdue" in err
        assert not UUID_RE.search(err)
        with purger_engine.connect() as other_run:
            other_run.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
            other_run.commit()
            skipped = Purger(purger_engine, sleep=_no_sleep).run()
            other_run.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            other_run.commit()
        assert skipped.skipped and skipped.overdue == 1 and not skipped.ok
        assert "1 overdue" in skipped.summary()
    finally:
        done = Purger(purger_engine, sleep=_no_sleep).run(only=request_id)
    assert done.done == 1 and done.overdue == 0 and done.ok  # purged: no longer overdue


@pytest.mark.db
def test_until_done_fails_while_an_erasure_is_still_open(
    db_engine: Engine,
    purger_engine: Engine,
    master_key: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DA-G1-8: ``--until-done`` (restore.sh) exits 1 when the run stops at its time limit
    with work left; without it the same run is not an error (the next hourly run resumes)."""
    from rag_app.purger import Purger, main

    user_id, _ = _make_user(db_engine, conversations=1, messages=2)
    request_id = _request(db_engine, user_id)
    try:
        assert Purger(purger_engine, sleep=_no_sleep, max_seconds=0).run().ok
        stopped = Purger(purger_engine, sleep=_no_sleep, max_seconds=0, until_done=True).run()
        assert stopped.open_left and stopped.unfinished and not stopped.ok
        rc = main(["purge", "--no-export", "--max-seconds", "0", "--until-done"])
        assert rc == 1 and "still open after this run" in capsys.readouterr().err
        assert _counts(db_engine, user_id)["tombstone"] == "pending"
    finally:
        Purger(purger_engine, sleep=_no_sleep).run(only=request_id)
    assert _counts(db_engine, user_id)["users"] == 0


# --- T11.2b.5: purge_orphaned / replay_deletions enqueue ---------------------------------


@pytest.mark.db
def test_purge_orphaned_enqueues_instead_of_bulk_deleting(
    db_engine: Engine, purger_engine: Engine, master_key: str
) -> None:
    from rag_app.purger import Purger

    user_id, _ = _make_user(db_engine, conversations=1, messages=4)
    with db_engine.begin() as conn:  # a key lost outside the app
        conn.execute(text("DELETE FROM user_keys WHERE user_id = :u"), {"u": user_id})
    with Session(db_engine) as session:
        assert erasure.purge_orphaned(session) >= 1
        assert erasure.purge_orphaned(session) == 0  # already queued: left alone
    queued = _counts(db_engine, user_id)
    assert queued["users"] == 1 and queued["messages"] == 4 and queued["tombstone"] == "pending"
    request_id = queued["request_id"]
    assert isinstance(request_id, uuid.UUID)
    assert Purger(purger_engine, sleep=_no_sleep).run(only=request_id).done == 1
    assert _counts(db_engine, user_id)["users"] == 0


@pytest.mark.db
def test_replay_deletions_re_applies_the_short_step_and_re_queues(
    db_engine: Engine, master_key: str
) -> None:
    from rag_app.db.models import DeletionRequest

    user_id, _ = _make_user(db_engine, conversations=1, messages=2)
    with Session(db_engine) as session:  # a restored DB: old `done` tombstone, account back
        session.add(DeletionRequest(user_id=user_id, status="done", completed_at=func.now()))
        session.commit()
        assert erasure.replay_deletions(session) >= 1
    state = _counts(db_engine, user_id)
    assert state["keys"] == 0 and state["tombstone"] == "pending" and state["messages"] == 2
    with db_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(text("users"))
                .where(text("id = :u AND email IS NULL AND deleted_at IS NOT NULL")),
                {"u": user_id},
            ).scalar()
            == 1
        )
