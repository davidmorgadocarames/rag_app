"""The batched, resumable erasure purger (T11.2b.4, D-ER) — run as ``secrag_purger``.

One run:

1. takes a session-level **advisory lock** (``pg_try_advisory_lock``) on its own connection:
   a second run while one is active only reports "skipped" (runs never overlap; a killed run
   releases the lock with its connection);
2. records a ``purger_runs`` row (start; at the end: finish, requests processed, errors, last
   error class — no personal data);
3. for every open tombstone (``pending``/``running``/``failed``, oldest first), claims it
   (``running``, attempts + 1) and follows ``rag_app.erasure.PURGE_STEPS`` leaf-first: each
   batch deletes up to ``batch_size`` rows in its OWN short transaction with ``SET LOCAL
   lock_timeout``/``statement_timeout`` and ``FOR UPDATE SKIP LOCKED``, and records the
   progress on the tombstone in that same transaction. A lock/statement timeout, or rows left
   that another transaction holds, backs off exponentially (never a tight loop) and retries;
   after ``MAX_RETRIES`` the tombstone is ``failed`` (error class only) and the next run
   retries it. A killed run leaves ``running`` + its progress: the next run resumes;
4. exports the tombstone list (``rag_app.tombstones`` format) on EVERY run — Blob
   ``tombstones/`` when ``TOMBSTONE_STORAGE_ACCOUNT`` is set (the Azure Job's managed
   identity), else a local folder (``--export-dir``, ``TOMBSTONE_EXPORT_DIR``, default
   ``.tombstones`` in the working directory; git-ignored) — then prunes exports older than
   ``TOMBSTONE_EXPORT_RETENTION_DAYS`` (30), always keeping the newest valid one (DA-F-8);
   a name it cannot judge is reported, nothing is deleted and the run exits 1 (DA-F2-1).

5. counts the tombstones still open (``pending``/``running``/``failed``) at the end — also
   when the run was skipped — and those open for more than ``PURGE_DEADLINE_HOURS`` (the
   202 promise): any **overdue** one is a WARNING and exit 1, so a purge Job that never runs
   on schedule, keeps failing or keeps being skipped fails visibly (DA-G1-1).

Output is counts only (never an id or an email). Exit 1 when a tombstone failed, the export
failed, an export name needs a look, or an erasure is overdue (the WARNING line says which);
with ``--until-done`` (``restore.sh``) also when ANY tombstone is still open after the run —
time limit, failure or a skipped run (DA-G1-8); 0 otherwise (also when another run held the
lock and nothing is overdue).

    python -m rag_app.erasure purge [--batch-size N] [--max-seconds S] [--pause-seconds P]
                                    [--export-dir DIR | --no-export] [--until-done]
    python -m rag_app.erasure purge-now <request-id> [...]   # one tombstone, now
    python -m rag_app.erasure loop [--interval S] [...]      # compose runner (hourly)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from rag_app.erasure import OPEN_STATUSES, PURGE_DEADLINE_HOURS, PURGE_STEPS

# Arbitrary, fixed: every purger (CLI, Job, compose loop) uses the same key.
ADVISORY_LOCK_KEY = 0x5EC_2A6_11B
BATCH_SIZE = 1000
BATCH_LOCK_TIMEOUT = "2s"
BATCH_STATEMENT_TIMEOUT = "5s"
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
# The Azure Job's replica timeout is 1800 s: stop claiming work well before it.
DEFAULT_MAX_SECONDS = 1500
DEFAULT_LOOP_INTERVAL = 3600

_TRANSIENT_SQLSTATES = frozenset({"55P03", "57014", "40P01", "40001"})  # lock, cancel, deadlock


@dataclass
class RunResult:
    skipped: bool = False
    processed: int = 0
    done: int = 0
    failed: int = 0
    stopped: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    batches: int = 0
    max_transaction_seconds: float = 0.0
    backoffs: int = 0
    export: str | None = None
    pruned: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    errors: int = 0
    last_error: str | None = None
    # After the run (DA-G1-1, DA-G1-8): tombstones still open, and those open for more than
    # PURGE_DEADLINE_HOURS; None when they could not be counted (that is an error).
    open_left: int | None = None
    overdue: int | None = None
    until_done: bool = False

    @property
    def unfinished(self) -> bool:
        """``--until-done``: the run did not leave every tombstone ``done``."""
        return self.until_done and (self.skipped or self.open_left != 0)

    @property
    def ok(self) -> bool:
        return (
            self.failed == 0
            and self.errors == 0
            and not self.problems
            and not self.overdue
            and not self.unfinished
        )

    def summary(self) -> str:
        still_open = (
            f"{self.open_left} still open, {self.overdue} overdue (> {PURGE_DEADLINE_HOURS} h)"
            if self.open_left is not None
            else "open tombstones not counted"
        )
        if self.skipped:
            return (
                "purger: another run holds the advisory lock — skipped (runs never overlap);"
                f" {still_open}"
            )
        rows = ", ".join(f"{k} {v}" for k, v in self.rows.items() if v) or "none"
        parts = [
            f"purger: {self.processed} request(s) processed: {self.done} done,"
            f" {self.failed} failed, {self.stopped} left running (time limit)",
            f"rows deleted: {rows}",
            f"{self.batches} batch transaction(s), longest {self.max_transaction_seconds:.3f} s,"
            f" {self.backoffs} back-off(s)",
            f"export: {self.export or 'none'}; pruned {len(self.pruned)} old export(s)",
            still_open,
        ]
        if self.last_error:
            parts.append(f"last error: {self.last_error}")
        return "; ".join(parts)


class ExportTarget(Protocol):
    def publish(self, engine: Engine) -> tuple[str, list[str], list[str]]:
        """Write one export; returns (where, pruned names, problems)."""
        ...


@dataclass
class LocalExport:
    directory: Path

    def publish(self, engine: Engine) -> tuple[str, list[str], list[str]]:
        from rag_app.tombstones import export_tombstones, prune_local_exports

        with Session(engine) as session:
            path = export_tombstones(session, self.directory)
        pruned, problems = prune_local_exports(self.directory)
        return str(path), pruned, problems


@dataclass
class BlobExport:
    client: object  # rag_app.backup_blob.ContainerClient

    def publish(self, engine: Engine) -> tuple[str, list[str], list[str]]:
        from rag_app.backup_blob import prune_tombstone_exports, upload_tombstone_export
        from rag_app.tombstones import export_tombstones

        with tempfile.TemporaryDirectory(prefix="secrag-tombstones-") as tmp:
            with Session(engine) as session:
                path = export_tombstones(session, Path(tmp))
            name = upload_tombstone_export(self.client, path)  # type: ignore[arg-type]
        pruned, problems = prune_tombstone_exports(self.client)  # type: ignore[arg-type]
        return f"blob {name}", pruned, problems


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


def _error_class(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return type(orig).__name__ if orig is not None else type(exc).__name__


class Purger:
    def __init__(
        self,
        engine: Engine,
        *,
        batch_size: int = BATCH_SIZE,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        export: ExportTarget | None = None,
        sleep: Callable[[float], None] = time.sleep,
        after_batch: Callable[[str, int], None] | None = None,
        pause_seconds: float = 0.0,
        until_done: bool = False,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.engine = engine
        self.until_done = until_done
        self.batch_size = batch_size
        self.max_seconds = max_seconds
        self.export = export
        self.sleep = sleep
        self.after_batch = after_batch
        # Optional pause after every batch that deleted rows: leaves headroom to the app on a
        # busy database (and lets the scale test kill a run mid-way deterministically).
        self.pause_seconds = pause_seconds
        self._deadline = 0.0

    # --- bookkeeping ------------------------------------------------------------------

    def _mark(self, request_id: uuid.UUID, status: str, error: str | None = None) -> None:
        completed = "now()" if status == "done" else "NULL"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE deletion_requests SET status = :s, last_error = :e,"
                    f" updated_at = now(), completed_at = {completed} WHERE id = :id"
                ),
                {"s": status, "e": error, "id": request_id},
            )

    def _backoff(self, attempt: int, result: RunResult) -> None:
        result.backoffs += 1
        self.sleep(min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)))

    # --- one tombstone ----------------------------------------------------------------

    def purge_one(self, request_id: uuid.UUID, result: RunResult) -> str:
        """done / failed / stopped (time limit; stays ``running``) / skip (not open)."""
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "UPDATE deletion_requests SET status = 'running', attempts = attempts + 1,"
                    " updated_at = now() WHERE id = :id AND status = ANY(:open)"
                    " RETURNING user_id"
                ),
                {"id": request_id, "open": list(OPEN_STATUSES)},
            ).first()
        if row is None:
            return "skip"
        user_id = row[0]
        result.processed += 1
        with self.engine.connect() as conn:
            live = conn.execute(
                text("SELECT deleted_at IS NULL FROM users WHERE id = :uid"), {"uid": user_id}
            ).scalar()
        if live:  # a tombstone for an account nobody marked deleted: never purge it
            self._mark(request_id, "failed", "account not marked deleted (deleted_at is NULL)")
            result.last_error = "account not marked deleted"
            return "failed"

        for step in PURGE_STEPS:
            attempt = 0
            while True:
                if time.monotonic() > self._deadline:
                    return "stopped"
                try:
                    t0 = time.monotonic()
                    with self.engine.begin() as conn:
                        conn.execute(text(f"SET LOCAL lock_timeout = '{BATCH_LOCK_TIMEOUT}'"))
                        conn.execute(
                            text(f"SET LOCAL statement_timeout = '{BATCH_STATEMENT_TIMEOUT}'")
                        )
                        deleted = conn.execute(
                            text(step.delete_batch), {"uid": user_id, "n": self.batch_size}
                        ).rowcount
                        if deleted:
                            conn.execute(
                                text(
                                    "UPDATE deletion_requests SET progress = jsonb_set(progress,"
                                    " ARRAY[CAST(:step AS text)], to_jsonb(COALESCE("
                                    "(progress->>CAST(:step AS text))::bigint, 0) + :n)),"
                                    " updated_at = now() WHERE id = :id"
                                ),
                                {"step": step.name, "n": deleted, "id": request_id},
                            )
                    result.batches += 1
                    result.max_transaction_seconds = max(
                        result.max_transaction_seconds, time.monotonic() - t0
                    )
                except OperationalError as exc:
                    if _sqlstate(exc) not in _TRANSIENT_SQLSTATES:
                        raise
                    attempt += 1
                    result.last_error = _error_class(exc)
                    if attempt > MAX_RETRIES:
                        self._mark(request_id, "failed", f"{step.name}: {_error_class(exc)}")
                        return "failed"
                    self._backoff(attempt, result)
                    continue
                result.rows[step.name] = result.rows.get(step.name, 0) + deleted
                if self.after_batch is not None:
                    self.after_batch(step.name, deleted)
                if deleted and self.pause_seconds > 0:
                    self.sleep(self.pause_seconds)
                if deleted:
                    attempt = 0
                    continue
                with self.engine.connect() as conn:
                    left = conn.execute(text(step.remaining), {"uid": user_id}).scalar()
                if not left:
                    break
                # rows left but every one was locked by another transaction (SKIP LOCKED)
                attempt += 1
                result.last_error = f"{step.name}: rows locked by another transaction"
                if attempt > MAX_RETRIES:
                    self._mark(request_id, "failed", result.last_error)
                    return "failed"
                self._backoff(attempt, result)
        self._mark(request_id, "done")
        return "done"

    # --- one run ----------------------------------------------------------------------

    def run(self, only: uuid.UUID | None = None) -> RunResult:
        result = RunResult(until_done=self.until_done)
        self._deadline = time.monotonic() + self.max_seconds
        with self.engine.connect() as lock_conn:
            got = lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            ).scalar()
            lock_conn.commit()
            if not got:
                result.skipped = True
            else:
                try:
                    self._run_locked(result, only)
                finally:
                    lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY}
                    )
                    lock_conn.commit()
        self._count_open(result)
        return result

    def _count_open(self, result: RunResult) -> None:
        """Open tombstones left, and those older than the 202 promise (DA-G1-1) — counted
        on every run, a skipped one too: a Job that never gets to work still fails loudly."""
        try:
            with self.engine.connect() as conn:
                overdue, open_left = conn.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE requested_at"
                        "   < now() - make_interval(hours => :h)), count(*)"
                        " FROM deletion_requests WHERE status = ANY(:open)"
                    ),
                    {"h": PURGE_DEADLINE_HOURS, "open": list(OPEN_STATUSES)},
                ).one()
        except SQLAlchemyError as exc:
            result.errors += 1
            result.last_error = f"count open: {_error_class(exc)}"
            return
        result.overdue, result.open_left = int(overdue), int(open_left)

    def _run_locked(self, result: RunResult, only: uuid.UUID | None) -> None:
        with self.engine.begin() as conn:
            run_id = conn.execute(
                text("INSERT INTO purger_runs DEFAULT VALUES RETURNING id")
            ).scalar_one()
        try:
            if only is not None:
                ids = [only]
            else:
                with self.engine.connect() as conn:
                    ids = list(
                        conn.execute(
                            text(
                                "SELECT id FROM deletion_requests WHERE status = ANY(:open)"
                                " ORDER BY requested_at, id"
                            ),
                            {"open": list(OPEN_STATUSES)},
                        ).scalars()
                    )
            for request_id in ids:
                if time.monotonic() > self._deadline:
                    break
                try:
                    outcome = self.purge_one(request_id, result)
                except SQLAlchemyError as exc:
                    result.errors += 1
                    result.last_error = _error_class(exc)
                    try:
                        self._mark(request_id, "failed", result.last_error)
                    except SQLAlchemyError:
                        pass
                    continue
                if outcome == "done":
                    result.done += 1
                elif outcome == "failed":
                    result.failed += 1
                elif outcome == "stopped":
                    result.stopped += 1
                    break
            if self.export is not None:
                try:
                    result.export, result.pruned, result.problems = self.export.publish(self.engine)
                except Exception as exc:  # noqa: BLE001 - SDK/OS errors: class name only
                    result.errors += 1
                    result.last_error = f"export: {type(exc).__name__}"
        finally:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE purger_runs SET finished_at = now(), requests_processed = :p,"
                        " errors = :e, last_error = :le WHERE id = :id"
                    ),
                    {
                        "p": result.processed,
                        "e": result.errors + result.failed,
                        "le": result.last_error,
                        "id": run_id,
                    },
                )


# --- CLI ------------------------------------------------------------------------------


def export_target(args: argparse.Namespace) -> ExportTarget | None:
    if args.no_export:
        return None
    if os.environ.get("TOMBSTONE_STORAGE_ACCOUNT"):
        from rag_app.backup_blob import container_client

        return BlobExport(container_client("TOMBSTONE_STORAGE_ACCOUNT"))
    directory = args.export_dir or Path(os.environ.get("TOMBSTONE_EXPORT_DIR") or ".tombstones")
    return LocalExport(directory)


def _report(result: RunResult) -> int:
    print(result.summary())
    for problem in result.problems:
        print(
            f"purger: WARNING: tombstone export {problem} — kept, and NO export was pruned;"
            " check it and remove it by hand",
            file=sys.stderr,
        )
    if result.overdue:
        print(
            f"purger: WARNING: {result.overdue} erasure(s) overdue — open for more than"
            f" {PURGE_DEADLINE_HOURS} h (the 202 promise); check that the purge Job runs on its"
            " schedule, and why it fails or is skipped",
            file=sys.stderr,
        )
    if result.unfinished:
        left = "unknown" if result.open_left is None else result.open_left
        print(
            f"purger: FAIL — {left} erasure(s) still open after this run (time limit, failure"
            " or another run holding the lock); --until-done needs every tombstone done —"
            " run it again",
            file=sys.stderr,
        )
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.erasure")
    sub = parser.add_subparsers(dest="cmd", required=True)
    commands = {
        "purge": sub.add_parser("purge", help="one purger run over every open tombstone"),
        "purge-now": sub.add_parser("purge-now", help="purge one tombstone now"),
        "loop": sub.add_parser("loop", help="run the purger every --interval seconds"),
    }
    commands["purge-now"].add_argument("request_id", type=uuid.UUID)
    commands["loop"].add_argument("--interval", type=int, default=DEFAULT_LOOP_INTERVAL)
    for p in commands.values():
        p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
        p.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
        p.add_argument("--pause-seconds", type=float, default=0.0)
        p.add_argument(
            "--until-done",
            action="store_true",
            help="exit 1 unless every tombstone is done after the run (restore.sh)",
        )
        where = p.add_mutually_exclusive_group()
        where.add_argument("--export-dir", type=Path)
        where.add_argument("--no-export", action="store_true")
    args = parser.parse_args(argv)

    from rag_app.db.session import make_engine

    try:
        target = export_target(args)
    except Exception as exc:  # noqa: BLE001 - e.g. missing AZURE_CLIENT_ID: message only
        print(f"purger: FAIL — {exc}", file=sys.stderr)
        return 1
    engine = make_engine()
    try:
        purger = Purger(
            engine,
            batch_size=args.batch_size,
            max_seconds=args.max_seconds,
            export=target,
            pause_seconds=args.pause_seconds,
            until_done=args.until_done,
        )
        if args.cmd == "purge":
            return _report(purger.run())
        if args.cmd == "purge-now":
            return _report(purger.run(only=args.request_id))
        while True:  # loop: the local compose runner (T11.2b.6)
            started = dt.datetime.now(dt.UTC)
            try:
                _report(purger.run())
            except SQLAlchemyError as exc:
                print(
                    f"purger: run failed — {_error_class(exc)} (secrag_purger login? set"
                    f" SECRAG_PURGER_PASSWORD and re-run db-roles); next run in {args.interval} s"
                )
            sys.stdout.flush()
            elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
            time.sleep(max(1.0, args.interval - elapsed))
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
