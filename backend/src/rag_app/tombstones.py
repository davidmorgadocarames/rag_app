"""Tombstones exported OUTSIDE the database, and the restore-time union (R4-3 + R5-2, T11.2.10).

A backup restored after an erasure would revive the erased account: the tombstone that
records the erasure is newer than the dump. So the full tombstone list is exported outside
the database (the hourly purge Job writes it to Blob ``tombstones/``; locally a git-ignored
folder, default ``.tombstones/`` at the repository root) and ``scripts/db/restore.sh``
applies the **union** of the restored tombstones and every exported one before the app
reopens, then ``replay_deletions`` re-erases those accounts.

Export format — one file per export, ``tombstones-<UTC yyyymmddThhmmssZ>.jsonl``, mode 0600,
one JSON object per line, nothing else::

    {"user_id": "<uuid>", "requested_at": "<ISO 8601 with offset>"}

The ids are opaque (no email, no name): a tombstone is not personal data on its own. A line
that does not match the format fails the whole read (a restore never guesses).

    python -m rag_app.tombstones export --dir DIR          # the purger's export (11.2b)
    python -m rag_app.tombstones validate --dir DIR        # format check, no database
    python -m rag_app.tombstones restore-union --dir DIR   # restore.sh, as the DB OWNER

``export`` and ``restore-union`` read ``DATABASE_URL`` only (``JobSettings``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rag_app.db.models import DeletionRequest, User
from rag_app.erasure import replay_deletions

FILE_RE = re.compile(r"^tombstones-(\d{8}T\d{6}Z)\.jsonl$")
FIELDS = frozenset({"user_id", "requested_at"})


class TombstoneExportError(ValueError):
    """An export file does not match the format; nothing is applied."""


def export_file_name(now: dt.datetime) -> str:
    return f"tombstones-{now.astimezone(dt.UTC):%Y%m%dT%H%M%SZ}.jsonl"


def export_tombstones(session: Session, directory: Path, now: dt.datetime | None = None) -> Path:
    """Write the FULL tombstone list to a new export file (atomic, 0600, directory 0700)."""
    now = now or dt.datetime.now(dt.UTC)
    rows = session.execute(
        select(DeletionRequest.user_id, DeletionRequest.requested_at).order_by(
            DeletionRequest.requested_at, DeletionRequest.user_id
        )
    ).all()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / export_file_name(now)
    tmp = directory / f".{target.name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for user_id, requested_at in rows:
            line = {"user_id": str(user_id), "requested_at": requested_at.isoformat()}
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def _parse_line(raw: str, where: str) -> tuple[uuid.UUID, dt.datetime]:
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        raise TombstoneExportError(f"{where}: not JSON") from None
    if not isinstance(item, dict) or set(item) != FIELDS:
        raise TombstoneExportError(f"{where}: expected exactly the keys {sorted(FIELDS)}")
    try:
        user_id = uuid.UUID(str(item["user_id"]))
        requested_at = dt.datetime.fromisoformat(str(item["requested_at"]))
    except ValueError:
        raise TombstoneExportError(f"{where}: bad user_id or requested_at") from None
    if requested_at.tzinfo is None:
        raise TombstoneExportError(f"{where}: requested_at has no UTC offset")
    return user_id, requested_at


def read_exports(directory: Path) -> tuple[dict[uuid.UUID, dt.datetime], int]:
    """Every tombstone in every export file of ``directory`` (earliest date wins) and the
    number of files read. The directory must exist (it may be empty: nothing was erased)."""
    if not directory.is_dir():
        raise TombstoneExportError(f"tombstone export directory {directory} does not exist")
    found: dict[uuid.UUID, dt.datetime] = {}
    files = sorted(p for p in directory.iterdir() if FILE_RE.match(p.name) and p.is_file())
    for path in files:
        for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            user_id, requested_at = _parse_line(raw, f"{path.name} line {n}")
            if user_id not in found or requested_at < found[user_id]:
                found[user_id] = requested_at
    return found, len(files)


def apply_union(session: Session, exported: dict[uuid.UUID, dt.datetime]) -> tuple[int, int]:
    """Add every exported tombstone the restored database lacks; returns (restored, added).

    Added tombstones are ``done`` like the ones ``erase_user`` writes today (11.2b's
    ``replay_deletions`` re-queues them as ``pending``)."""
    restored = set(session.scalars(select(DeletionRequest.user_id)))
    now = dt.datetime.now(dt.UTC)
    missing = sorted(set(exported) - restored)
    for user_id in missing:
        session.add(
            DeletionRequest(
                user_id=user_id,
                requested_at=exported[user_id],
                status="done",
                completed_at=now,
            )
        )
    session.flush()
    return len(restored), len(missing)


def restore_union(session: Session, directory: Path) -> dict[str, int]:
    """The restore step: union of restored + exported tombstones, then ``replay_deletions``
    (one commit for the union, then the replay's own). Counts only — never an id."""
    exported, files = read_exports(directory)
    users_before = session.scalar(select(func.count()).select_from(User)) or 0
    restored, added = apply_union(session, exported)
    session.commit()
    reerased = replay_deletions(session)
    users_after = session.scalar(select(func.count()).select_from(User)) or 0
    tombstones = session.scalar(select(func.count()).select_from(DeletionRequest)) or 0
    return {
        "export_files": files,
        "exported": len(exported),
        "restored": restored,
        "added": added,
        "tombstones": tombstones,
        "reerased": reerased,
        "users_before": users_before,
        "users_after": users_after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.tombstones")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("export", "restore-union", "validate"):
        sub.add_parser(name).add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "validate":  # no database: restore.sh checks the exports before restoring
        try:
            exported, files = read_exports(args.dir)
        except TombstoneExportError as exc:
            print(f"tombstones: REFUSED — {exc}", file=sys.stderr)
            return 2
        print(f"tombstones: {len(exported)} exported tombstone(s) in {files} file(s) — valid")
        return 0

    from rag_app.db.session import make_engine

    engine = make_engine()
    try:
        with Session(engine) as session:
            if args.cmd == "export":
                path = export_tombstones(session, args.dir)
                count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)
                print(f"tombstones: exported {count} to {path}")
                return 0
            try:
                c = restore_union(session, args.dir)
            except TombstoneExportError as exc:
                print(f"tombstones: REFUSED — {exc}; nothing applied", file=sys.stderr)
                return 2
            print(
                f"tombstones: {c['restored']} restored in the database, {c['exported']} in"
                f" {c['export_files']} export file(s), {c['added']} added (union ="
                f" {c['tombstones']}); replay_deletions re-erased {c['reerased']} account(s);"
                f" users {c['users_before']} -> {c['users_after']}"
            )
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
