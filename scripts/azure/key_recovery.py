"""Master-key recovery for accounts whose data key no longer unwraps (D-2026-09-29-2 (b)).

Run it through ``scripts/azure/key_recovery.sh`` (backend venv, ``python -I -B``, core dumps
off). It reads CANDIDATE old master keys from a private local file, tests them against the
stored ``user_keys.wrapped_key`` of the accounts, and prints only OK/KO lines:

    key_recovery.sh init                 create ~/.secrag-recovery/candidates (dir 0700, file
                                         0600, empty) and print how to fill it
    key_recovery.sh check --accounts all|1,2 [--expect-total N]
                          [--current-key-from-app APP | --current-key-from-env-file PATH]
    key_recovery.sh rewrap --account I --candidate J [--expect-total N]
                           (--current-key-from-app APP | --current-key-from-env-file PATH)
                           [--apply --i-have-a-pg-dump]
    key_recovery.sh erase --account I --expect-total N --current-key-from-env-file PATH
                          [--apply --i-have-a-snapshot]          (LOCAL development DB only)
    key_recovery.sh shred                overwrite (random, then zeros) and delete the
                                         candidate file and its directory

Database: libpq environment only (``PGHOST``/``PGPORT``/``PGUSER``/``PGDATABASE`` + a
password from ``PGPASSFILE``/``PGPASSWORD``). On Azure the tool runs INSIDE
``scripts/azure/db-tunnel.sh`` (read-only by default; ``--read-write`` only for
``rewrap --apply``), so wrapped keys travel only from the server into this process' memory.
Locally it points at a throwaway database restored from a snapshot, or at the development DB.

Accounts are labelled ``account #i`` = position by ``users.created_at, users.id`` among the
accounts that have a key, and every command prints the total ``N`` first (``accounts: N in
total``). A new account is appended at the end (its ``created_at`` is the latest); an erased
account shifts the labels after it — so ``--expect-total N`` (optional for check/rewrap,
mandatory for erase) refuses when the total differs from what ``check`` printed. Never an id,
email or hash is printed. The candidate keys, the current master key, the wrapped keys and
the unwrapped data keys are NEVER printed, logged, written to a file, put on argv or in the
environment: every error prints only an exception CLASS name; a malformed candidate prints
"candidate #j: not a valid Fernet key", a line that is not UTF-8 "candidate #j: unreadable".
``check`` runs in a READ ONLY transaction.

``rewrap`` (PHASE_TASKS row 40, after the row-40 ``pg_dump``): in ONE transaction, locks
account I's key row, refuses unless the current key cannot unwrap it and candidate J can,
unwraps with J, wraps the same data key under the CURRENT master key, verifies, and — only
with ``--apply --i-have-a-pg-dump`` — updates exactly that one row. Without ``--apply`` it
prints "dry run: would update 1 row" and rolls back. The master key is never switched back.

``erase`` (D-2026-09-30-2, local only — on Azure the owner deletes the account in the app):
for an account that NO key can unwrap. In ONE transaction it checks the total against
``--expect-total``, locks the account, and refuses unless the current key AND every
candidate fail on that very blob (it also refuses while any candidate line is unreadable or
malformed); then it erases the account through the app's own erasure path
(``rag_app.erasure.erase_user``: user row deleted with its key, conversations and messages by
cascade, tombstone ``done`` written, one commit). Without ``--apply`` it is a read-only dry
run. It refuses a non-local ``PGHOST``/``PGHOSTADDR``, ``PGSERVICE``, a run inside the tunnel,
and a schema older than migration 0005 (the app's tombstone columns).

If an error happens after COMMIT was sent (``--apply``), the tool cannot know whether the
change was committed: it prints "state unknown — run check again" instead of "nothing
changed".

Candidate file format: one key per line (url-safe base64 of 32 bytes); blank lines and
lines starting with ``#`` are ignored; a line like ``DATA_MASTER_KEY=<key>`` or
``data-master-key=<key>`` (as pasted from an .env file or a shell history) is accepted. Each
line is decoded on its own as UTF-8 (a leading BOM and CR line ends are removed, so a file
saved by a Windows editor works). The file must be a regular file, mode 0600, in a directory
of mode 0700, both owned by you, no symlink anywhere on the path, NOT inside any git work
tree and NOT on a Windows drive (/mnt, 9p/drvfs). Fill it without the shell history:
``nano ~/.secrag-recovery/candidates`` or ``cat > ~/.secrag-recovery/candidates``, paste,
then Ctrl-D. ``--candidates PATH`` (before the subcommand) uses another file.

The process makes itself non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``: no core dump even with
a pipe ``core_pattern``, no same-user ptrace or ``/proc/<pid>/mem``) and sets
``RLIMIT_CORE`` to 0 before it reads any key.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import resource
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

DEFAULT_DIR = Path.home() / ".secrag-recovery"
DEFAULT_FILE = DEFAULT_DIR / "candidates"
WINDOWS_FS = {"9p", "v9fs", "drvfs"}
PREFIXES = ("DATA_MASTER_KEY=", "data-master-key=", "export DATA_MASTER_KEY=")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
TUNNEL_APPNAME = "secrag-db-tunnel"  # scripts/azure/db-tunnel.sh sets PGAPPNAME to this

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4

TOTAL_SQL = "SELECT count(*) FROM user_keys uk JOIN users u ON u.id = uk.user_id"

ACCOUNTS_SQL = """
SELECT n, user_id, wrapped_key FROM (
    SELECT row_number() OVER (ORDER BY u.created_at, u.id) AS n, uk.user_id, uk.wrapped_key
      FROM user_keys uk JOIN users u ON u.id = uk.user_id
) ranked
WHERE %(all)s OR n = ANY(%(wanted)s::bigint[])
ORDER BY n
"""

# True once COMMIT of a write may have been sent: from then on an error means "state unknown".
_STATE = {"write_sent": False}


class Refusal(Exception):
    """A safety rule refused the run; the message never contains key material."""


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# --- process hardening ----------------------------------------------------------------------


def _harden() -> None:
    """No core dump, no ptrace, no /proc/<pid>/mem for this process (DA-E-2)."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if (
            libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0
            or libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0
        ):
            raise Refusal("cannot make the process non-dumpable (prctl PR_SET_DUMPABLE)")


# --- candidate file -----------------------------------------------------------------------


def _mounts() -> list[tuple[str, str]]:
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return []
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((parts[1].replace("\\040", " "), parts[2]))
    return mounts


def _fs_type(path: Path) -> str:
    best, fstype = "", ""
    for mountpoint, kind in _mounts():
        prefix = mountpoint.rstrip("/") + "/"
        if (str(path) == mountpoint or str(path).startswith(prefix)) and len(mountpoint) > len(
            best
        ):
            best, fstype = mountpoint, kind
    return fstype


def _inside_git_work_tree(path: Path) -> bool:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return True
    return False


def _check_location(path: Path) -> None:
    """Refuse a symlinked path, a git work tree, /mnt and Windows filesystems."""
    absolute = Path(os.path.abspath(path))
    if Path(os.path.realpath(absolute)) != absolute:
        raise Refusal("the candidate file path contains a symlink")
    if str(absolute).startswith("/mnt/") or _fs_type(absolute.parent) in WINDOWS_FS:
        raise Refusal("the candidate file is on a Windows drive (/mnt, 9p/drvfs)")
    if _inside_git_work_tree(absolute.parent):
        raise Refusal("the candidate file is inside a git work tree")


def _check_owner_mode(path: Path, want: int, kind: str) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise Refusal(f"the candidate {kind} is a symlink")
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise Refusal("the candidate file is not a regular file")
    if kind == "directory" and not stat.S_ISDIR(info.st_mode):
        raise Refusal("the candidate directory is not a directory")
    if info.st_uid != os.getuid():
        raise Refusal(f"the candidate {kind} is not owned by you")
    mode = stat.S_IMODE(info.st_mode)
    if mode != want:
        raise Refusal(f"the candidate {kind} has mode {mode:o}; it must be {want:o}")


def _check_file(path: Path) -> None:
    _check_location(path)
    if not os.path.lexists(path.parent):
        raise Refusal("no candidate directory — run: key_recovery.sh init")
    _check_owner_mode(path.parent, 0o700, "directory")
    if not os.path.lexists(path):
        raise Refusal("no candidate file — run: key_recovery.sh init")
    _check_owner_mode(path, 0o600, "file")


def _fernet(key: str):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    raw = base64.urlsafe_b64decode(key.encode("ascii"))
    if len(raw) != 32:
        raise ValueError
    return Fernet(key.encode("ascii"))


def _parse_line(line: str) -> str:
    value = line.strip()
    for prefix in PREFIXES:
        if prefix in value:
            value = value.split(prefix, 1)[1]
    return value.strip().strip("'\"").strip()


def load_candidates(path: Path) -> list[object | None]:
    """The candidates as Fernet objects (None = unreadable or malformed, reported by index
    only). Each line is decoded on its own (DA-E-3): one bad line never hides the others."""
    _check_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        raw_lines = handle.read().split(b"\n")
    candidates: list[object | None] = []
    problems: list[tuple[int, str]] = []
    for raw in raw_lines:
        try:
            line = raw.decode("utf-8-sig").replace("\r", "")
        except UnicodeDecodeError:
            candidates.append(None)
            problems.append((len(candidates), "unreadable (not UTF-8)"))
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            candidates.append(_fernet(_parse_line(line)))
        except (ValueError, binascii.Error, UnicodeError):
            candidates.append(None)
            problems.append((len(candidates), "not a valid Fernet key"))
    for j, why in problems:
        out(f"candidate #{j}: {why} (skipped)")
    if not candidates:
        raise Refusal("the candidate file has no candidates")
    return candidates


def init(path: Path) -> None:
    _check_location(path)
    directory = path.parent
    if not os.path.lexists(directory):
        os.mkdir(directory, 0o700)
    os.chmod(directory, 0o700, follow_symlinks=False)
    _check_owner_mode(directory, 0o700, "directory")
    if not os.path.lexists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    _check_owner_mode(path, 0o600, "file")
    out(f"candidate file ready: {path} (directory 0700, file 0600)")
    out("Fill it WITHOUT the shell history — one key per line; '#' comments and blank lines")
    out("are ignored; lines like DATA_MASTER_KEY=<key> are accepted:")
    out(f"  nano {path}")
    out(f"  or: cat > {path}   then paste the keys and press Ctrl-D")
    out("Never pass a key as an argument or an environment variable. When done:")
    out("  key_recovery.sh shred")


def shred(path: Path) -> None:
    _check_location(path)
    directory = path.parent
    if not os.path.lexists(directory):
        out("nothing to shred")
        return
    _check_owner_mode(directory, 0o700, "directory")
    removed = 0
    for entry in sorted(directory.iterdir()):
        info = os.lstat(entry)
        if stat.S_ISDIR(info.st_mode):
            raise Refusal("the candidate directory contains a subdirectory — remove it by hand")
        if stat.S_ISREG(info.st_mode):
            size = info.st_size
            fd = os.open(entry, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                for filler in (lambda n: os.urandom(n), lambda n: b"\0" * n):
                    os.lseek(fd, 0, os.SEEK_SET)
                    remaining = size
                    while remaining > 0:
                        chunk = min(remaining, 65536)
                        os.write(fd, filler(chunk))
                        remaining -= chunk
                    os.fsync(fd)
            finally:
                os.close(fd)
        os.unlink(entry)
        removed += 1
    os.rmdir(directory)
    out(f"shredded and removed {removed} file(s) and the directory {directory}")
    out("note: on journaling/SSD storage an overwrite is best effort; the WSL disk image is")
    out("not encrypted — keep the old keys only in the password manager.")


# --- current master key (never printed) ---------------------------------------------------


def current_key(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    if getattr(args, "current_key_from_app", None):
        rg = args.rg or os.environ.get("DB_TUNNEL_RG") or "rg-secrag"
        proc = subprocess.run(
            [
                "az",
                "containerapp",
                "secret",
                "show",
                "-g",
                rg,
                "-n",
                args.current_key_from_app,
                "--secret-name",
                "data-master-key",
                "--query",
                "value",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise Refusal("cannot read the app's data-master-key secret (az failed)")
        value = proc.stdout.strip()
    elif args.current_key_from_env_file:
        env_path = Path(args.current_key_from_env_file)
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
        if mode & 0o077:
            raise Refusal(f"the env file has mode {mode:o}; chmod 600 it first")
        value = ""
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("DATA_MASTER_KEY="):
                value = _parse_line(line)
        if not value:
            raise Refusal("no DATA_MASTER_KEY in the env file")
    else:
        return None
    try:
        return _fernet(value)
    except (ValueError, binascii.Error, UnicodeError):
        raise Refusal("the current DATA_MASTER_KEY is not a valid Fernet key") from None


# --- database (libpq environment; the tunnel sets it on Azure) -------------------------------


def _connect() -> Any:
    import psycopg

    return psycopg.connect("", connect_timeout=20, application_name="secrag-key-recovery")


def _accounts(conn, wanted: list[int] | None):  # type: ignore[no-untyped-def]
    rows = conn.execute(ACCOUNTS_SQL, {"all": wanted is None, "wanted": wanted or []}).fetchall()
    return [(int(n), user_id, bytes(blob)) for n, user_id, blob in rows]


def _total(conn) -> int:  # type: ignore[no-untyped-def]
    return int(conn.execute(TOTAL_SQL).fetchone()[0])


def _expect_total(total: int, expected: int | None) -> None:
    """Labels are positions: refuse when accounts were added or erased since `check`."""
    out(f"accounts: {total} in total (#1…#{total} by creation time, then id)")
    if expected is not None and total != expected:
        raise Refusal(
            f"the database has {total} account(s) with a key, --expect-total says {expected}"
            " — accounts were added or erased since the check, so the labels may have"
            " shifted; run check again"
        )


def _unwraps(fernet, blob: bytes) -> bool:  # type: ignore[no-untyped-def]
    try:
        fernet.decrypt(blob)
        return True
    except Exception:  # noqa: BLE001 - InvalidToken or a malformed blob
        return False


def check(args: argparse.Namespace) -> int:
    candidates = load_candidates(args.candidates)
    current = current_key(args)
    wanted = None if args.accounts == "all" else _parse_accounts(args.accounts)
    with _connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        total = _total(conn)
        accounts = _accounts(conn, wanted)
        conn.rollback()
    _expect_total(total, args.expect_total)
    readable = matched = unmatched = 0
    for n, _user_id, blob in accounts:
        if current is not None and _unwraps(current, blob):
            out(f"account #{n}: current key OK")
            readable += 1
            continue
        if current is not None:
            out(f"account #{n}: current key KO")
        hit = False
        for j, candidate in enumerate(candidates, start=1):
            if candidate is None:
                continue
            ok = _unwraps(candidate, blob)
            hit = hit or ok
            out(f"account #{n}: candidate #{j} {'OK' if ok else 'KO'}")
        matched += hit
        unmatched += not hit
    out(
        f"summary: {len(accounts)} account(s) checked of {total}; "
        + (f"{readable} readable with the current key; " if current is not None else "")
        + f"{matched} matched a candidate; {unmatched} without a match"
    )
    return 0


def rewrap(args: argparse.Namespace) -> int:
    if args.apply and not args.i_have_a_pg_dump:
        raise Refusal("--apply needs --i-have-a-pg-dump (row 40: pg_dump first)")
    candidates = load_candidates(args.candidates)
    if not 1 <= args.candidate <= len(candidates) or candidates[args.candidate - 1] is None:
        raise Refusal(f"candidate #{args.candidate} does not exist or is not a valid key")
    old = candidates[args.candidate - 1]
    current = current_key(args)
    if current is None:
        raise Refusal("rewrap needs the current master key (--current-key-from-…)")
    with _connect() as conn:
        with conn.transaction(force_rollback=not args.apply):
            _expect_total(_total(conn), args.expect_total)
            rows = conn.execute(
                ACCOUNTS_SQL,
                {"all": False, "wanted": [args.account]},
            ).fetchall()
            if len(rows) != 1:
                raise Refusal(f"account #{args.account} does not exist")
            _n, user_id, blob = rows[0]
            # Lock the row only for a real update: a dry run also works in the tunnel's
            # read-only session.
            lock = " FOR UPDATE" if args.apply else ""
            locked = conn.execute(
                "SELECT wrapped_key FROM user_keys WHERE user_id = %s" + lock, (user_id,)
            ).fetchone()
            if locked is None or bytes(locked[0]) != bytes(blob):
                raise Refusal(f"account #{args.account} changed while reading — run it again")
            blob = bytes(locked[0])
            if _unwraps(current, blob):
                out(
                    f"account #{args.account}: already readable with the current key"
                    " — nothing to do"
                )
                return 0
            try:
                data_key = bytearray(old.decrypt(blob))
            except Exception:  # noqa: BLE001
                out(f"account #{args.account}: candidate #{args.candidate} KO — nothing changed")
                return 1
            try:
                new_blob = current.encrypt(bytes(data_key))
                if current.decrypt(new_blob) != bytes(data_key):
                    raise Refusal("verification of the re-wrapped key failed — nothing changed")
                if not args.apply:
                    out(f"dry run: would update 1 row (account #{args.account}); nothing changed")
                    return 0
                updated = conn.execute(
                    "UPDATE user_keys SET wrapped_key = %s WHERE user_id = %s AND wrapped_key = %s",
                    (new_blob, user_id, blob),
                ).rowcount
                if updated != 1:
                    raise Refusal(f"expected to update 1 row, would update {updated} — rolled back")
                # Leaving the transaction block sends COMMIT: from here an error cannot tell
                # whether the row was changed (DA-E-4).
                _STATE["write_sent"] = True
            finally:
                for i in range(len(data_key)):
                    data_key[i] = 0
    out(f"account #{args.account}: re-wrapped under the current master key (1 row updated)")
    return 0


# --- erase (local development DB only; D-2026-09-30-2) ----------------------------------------


def _local_only() -> None:
    """erase never runs against Azure: the owner deletes that account in the app instead."""
    if os.environ.get("PGAPPNAME") == TUNNEL_APPNAME:
        raise Refusal("erase is local-only; it does not run inside db-tunnel.sh (D-2026-09-30-2)")
    if os.environ.get("PGSERVICE") or os.environ.get("PGSERVICEFILE"):
        raise Refusal("erase is local-only; unset PGSERVICE/PGSERVICEFILE and use PGHOST")
    for var in ("PGHOST", "PGHOSTADDR"):
        for host in filter(None, os.environ.get(var, "").split(",")):
            if host not in LOCAL_HOSTS and not host.startswith("/"):
                raise Refusal(
                    f"erase is local-only; {var} must be 127.0.0.1, localhost, ::1 or a socket"
                    " directory (on Azure the owner deletes the account in the app)"
                )


def _app_erasure() -> tuple[Any, Any]:
    """The app's own erasure path and tombstone model, from this checkout's backend."""
    src = Path(os.path.abspath(__file__)).parents[2] / "backend" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from rag_app.db.models import DeletionRequest
    from rag_app.erasure import erase_user

    return erase_user, DeletionRequest


def erase(args: argparse.Namespace) -> int:
    if args.apply and not args.i_have_a_snapshot:
        raise Refusal("--apply needs --i-have-a-snapshot (pg_dump or volume snapshot first)")
    _local_only()
    candidates = load_candidates(args.candidates)
    bad = [j for j, c in enumerate(candidates, start=1) if c is None]
    if bad:
        raise Refusal(
            "candidate(s) " + ", ".join(f"#{j}" for j in bad) + " could not be read — erase"
            " needs every candidate tried; fix or remove those lines first"
        )
    current = current_key(args)
    erase_user, deletion_request = _app_erasure()

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    engine = create_engine("postgresql+psycopg://", creator=_connect, poolclass=NullPool)
    try:
        with Session(engine) as session:
            conn = session.connection()
            if not args.apply:
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            columns = {
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = current_schema() AND table_name = 'deletion_requests'"
                )
            }
            if not set(deletion_request.__table__.columns.keys()) <= columns:
                raise Refusal(
                    "the schema is older than the app's erasure path (migration 0005) — run"
                    " db-roles and migrate first"
                )
            total = int(conn.exec_driver_sql(TOTAL_SQL).scalar_one())
            _expect_total(total, args.expect_total)
            rows = conn.exec_driver_sql(
                ACCOUNTS_SQL, {"all": False, "wanted": [args.account]}
            ).fetchall()
            if len(rows) != 1:
                raise Refusal(f"account #{args.account} does not exist")
            _n, user_id, blob = rows[0]
            lock = " FOR UPDATE" if args.apply else ""
            locked = conn.exec_driver_sql(
                "SELECT uk.wrapped_key FROM user_keys uk JOIN users u ON u.id = uk.user_id"
                " WHERE uk.user_id = %(id)s" + lock,
                {"id": user_id},
            ).fetchone()
            if locked is None or bytes(locked[0]) != bytes(blob):
                raise Refusal(f"account #{args.account} changed while reading — run it again")
            blob = bytes(locked[0])
            if _unwraps(current, blob):
                raise Refusal(
                    f"account #{args.account} is readable with the current key — erase never"
                    " touches a readable account"
                )
            for j, candidate in enumerate(candidates, start=1):
                if _unwraps(candidate, blob):
                    raise Refusal(
                        f"candidate #{j} unwraps account #{args.account} — recover it with"
                        " rewrap instead of erasing it"
                    )
            out(
                f"account #{args.account}: current key KO, every candidate"
                f" ({len(candidates)}) KO"
            )
            if not args.apply:
                session.rollback()
                out(
                    f"dry run: would erase account #{args.account} through the app's erasure"
                    " path (user row, key, conversations, messages; tombstone); nothing changed"
                )
                return 0

            def _commit_sent(_session: Session) -> None:
                _STATE["write_sent"] = True

            event.listen(session, "before_commit", _commit_sent)
            erase_user(session, user_id)  # deletes + writes the tombstone + commits
        with Session(engine) as session:
            conn = session.connection()
            still = conn.exec_driver_sql(
                "SELECT count(*) FROM users WHERE id = %(id)s", {"id": user_id}
            ).scalar_one()
            tombstones = conn.exec_driver_sql(
                "SELECT count(*) FROM deletion_requests WHERE user_id = %(id)s", {"id": user_id}
            ).scalar_one()
            remaining = int(conn.exec_driver_sql(TOTAL_SQL).scalar_one())
            session.rollback()
        if still or tombstones != 1:
            raise RuntimeError("erase not visible after commit")
    finally:
        engine.dispose()
    out(
        f"account #{args.account}: erased through the app's erasure path (tombstone written);"
        f" {remaining} account(s) remain — labels after #{args.account} moved down by one"
    )
    return 0


def _parse_accounts(raw: str) -> list[int]:
    try:
        values = sorted({int(x) for x in raw.split(",") if x.strip()})
    except ValueError:
        raise Refusal("--accounts takes 'all' or a comma list of account numbers") from None
    if not values or values[0] < 1:
        raise Refusal("--accounts takes 'all' or a comma list of account numbers")
    return values


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="key_recovery.sh", add_help=True)
    p.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_FILE,
        help="candidate file (default: %(default)s)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("shred")
    for name in ("check", "rewrap"):
        s = sub.add_parser(name)
        src = s.add_mutually_exclusive_group(required=name == "rewrap")
        src.add_argument("--current-key-from-app", metavar="APP")
        src.add_argument("--current-key-from-env-file", metavar="PATH")
        s.add_argument("--rg", help="resource group for --current-key-from-app")
        s.add_argument(
            "--expect-total", type=int, metavar="N", help="refuse unless N accounts exist"
        )
        if name == "check":
            s.add_argument("--accounts", required=True, help="'all' or e.g. 1,3")
        else:
            s.add_argument("--account", type=int, required=True)
            s.add_argument("--candidate", type=int, required=True)
            s.add_argument("--apply", action="store_true")
            s.add_argument("--i-have-a-pg-dump", action="store_true")
    e = sub.add_parser("erase", help="local development DB only (D-2026-09-30-2)")
    e.add_argument("--account", type=int, required=True)
    e.add_argument("--expect-total", type=int, required=True, metavar="N")
    e.add_argument("--current-key-from-env-file", metavar="PATH", required=True)
    e.add_argument("--apply", action="store_true")
    e.add_argument("--i-have-a-snapshot", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.candidates = Path(os.path.abspath(os.path.expanduser(str(args.candidates))))
    if args.cmd == "init":
        init(args.candidates)
        return 0
    if args.cmd == "shred":
        shred(args.candidates)
        return 0
    if args.cmd == "check":
        return check(args)
    if args.cmd == "erase":
        return erase(args)
    return rewrap(args)


def main(argv: list[str] | None = None) -> int:
    _STATE["write_sent"] = False
    try:
        _harden()  # before any key is read
        return run(argv)
    except SystemExit as exc:  # argparse (usage errors carry no key material)
        return int(exc.code or 0)
    except Refusal as exc:
        sys.stderr.write(f"key_recovery: REFUSED — {exc}\n")
        return 2
    except BaseException as exc:  # noqa: BLE001 - never a traceback or a message
        what = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else f"error ({type(exc).__name__})"
        )
        if _STATE["write_sent"]:
            sys.stderr.write(
                f"key_recovery: {what} after COMMIT was sent — state unknown: the change may"
                " or may not be committed; run check again\n"
            )
        else:
            sys.stderr.write(f"key_recovery: {what} — nothing changed\n")
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    sys.exit(main())
