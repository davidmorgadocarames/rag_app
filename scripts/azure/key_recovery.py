"""Master-key recovery for accounts whose data key no longer unwraps (D-2026-09-29-2 (b)).

Run it through ``scripts/azure/key_recovery.sh`` (backend venv, ``python -I -B``, core dumps
off). It reads CANDIDATE old master keys from a private local file, tests them against the
stored ``user_keys.wrapped_key`` of the accounts, and prints only OK/KO lines:

    key_recovery.sh init                 create ~/.secrag-recovery/candidates (dir 0700, file
                                         0600, empty) and print how to fill it
    key_recovery.sh check --accounts all|1,2 [--current-key-from-app APP |
                                              --current-key-from-env-file PATH]
    key_recovery.sh rewrap --account N --candidate J (--current-key-from-app APP |
                           --current-key-from-env-file PATH) [--apply --i-have-a-pg-dump]
    key_recovery.sh shred                overwrite (random, then zeros) and delete the
                                         candidate file and its directory

Database: libpq environment only (``PGHOST``/``PGPORT``/``PGUSER``/``PGDATABASE`` + a
password from ``PGPASSFILE``/``PGPASSWORD``). On Azure the tool runs INSIDE
``scripts/azure/db-tunnel.sh`` (read-only by default; ``--read-write`` only for
``rewrap --apply``), so wrapped keys travel only from the server into this process' memory.
Locally it points at a throwaway database restored from a snapshot.

Accounts are labelled ``account #i`` = position by ``users.created_at, users.id`` — never an
id, email or hash. The candidate keys, the current master key, the wrapped keys and the
unwrapped data keys are NEVER printed, logged, written to a file, put on argv or in the
environment: every error prints only an exception CLASS name; a malformed candidate prints
"candidate #j: not a valid Fernet key". ``check`` runs in a READ ONLY transaction.

``rewrap`` (PHASE_TASKS row 40, after the row-40 ``pg_dump``): in ONE transaction, locks
account N's key row, refuses unless the current key cannot unwrap it and candidate J can,
unwraps with J, wraps the same data key under the CURRENT master key, verifies, and — only
with ``--apply --i-have-a-pg-dump`` — updates exactly that one row. Without ``--apply`` it
prints "dry run: would update 1 row" and rolls back. The master key is never switched back.

Candidate file format: one key per line (url-safe base64 of 32 bytes); blank lines and
lines starting with ``#`` are ignored; a line like ``DATA_MASTER_KEY=<key>`` or
``data-master-key=<key>`` (as pasted from an .env file or a shell history) is accepted.
The file must be a regular file, mode 0600, in a directory of mode 0700, both owned by you,
no symlink anywhere on the path, NOT inside any git work tree and NOT on a Windows drive
(/mnt, 9p/drvfs). Fill it without the shell history: ``nano ~/.secrag-recovery/candidates``
or ``cat > ~/.secrag-recovery/candidates``, paste, then Ctrl-D.
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

sys.dont_write_bytecode = True

DEFAULT_DIR = Path.home() / ".secrag-recovery"
DEFAULT_FILE = DEFAULT_DIR / "candidates"
WINDOWS_FS = {"9p", "v9fs", "drvfs"}
PREFIXES = ("DATA_MASTER_KEY=", "data-master-key=", "export DATA_MASTER_KEY=")

ACCOUNTS_SQL = """
SELECT n, user_id, wrapped_key FROM (
    SELECT row_number() OVER (ORDER BY u.created_at, u.id) AS n, uk.user_id, uk.wrapped_key
      FROM user_keys uk JOIN users u ON u.id = uk.user_id
) ranked
WHERE %(all)s OR n = ANY(%(wanted)s::bigint[])
ORDER BY n
"""


class Refusal(Exception):
    """A safety rule refused the run; the message never contains key material."""


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


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
    """The candidates as Fernet objects (None = malformed, reported by index only)."""
    _check_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as handle:
        lines = handle.read().splitlines()
    candidates: list[object | None] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            candidates.append(_fernet(_parse_line(line)))
        except (ValueError, binascii.Error, UnicodeError):
            candidates.append(None)
    for j, candidate in enumerate(candidates, start=1):
        if candidate is None:
            out(f"candidate #{j}: not a valid Fernet key (skipped)")
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
    if args.current_key_from_app:
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
        for line in env_path.read_text(encoding="utf-8").splitlines():
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


def _connect():  # type: ignore[no-untyped-def]
    import psycopg

    return psycopg.connect("", connect_timeout=20, application_name="secrag-key-recovery")


def _accounts(conn, wanted: list[int] | None):  # type: ignore[no-untyped-def]
    rows = conn.execute(ACCOUNTS_SQL, {"all": wanted is None, "wanted": wanted or []}).fetchall()
    return [(int(n), user_id, bytes(blob)) for n, user_id, blob in rows]


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
        accounts = _accounts(conn, wanted)
        conn.rollback()
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
        f"summary: {len(accounts)} account(s) checked; "
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
                    f"account #{args.account}: already readable with the current key — nothing to do"
                )
                return 0
            try:
                data_key = bytearray(old.decrypt(blob))  # type: ignore[attr-defined]
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
            finally:
                for i in range(len(data_key)):
                    data_key[i] = 0
    out(f"account #{args.account}: re-wrapped under the current master key (1 row updated)")
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
    p.add_argument("--candidates", type=Path, default=DEFAULT_FILE, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("shred")
    for name in ("check", "rewrap"):
        s = sub.add_parser(name)
        src = s.add_mutually_exclusive_group(required=name == "rewrap")
        src.add_argument("--current-key-from-app", metavar="APP")
        src.add_argument("--current-key-from-env-file", metavar="PATH")
        s.add_argument("--rg", help="resource group for --current-key-from-app")
        if name == "check":
            s.add_argument("--accounts", required=True, help="'all' or e.g. 1,3")
        else:
            s.add_argument("--account", type=int, required=True)
            s.add_argument("--candidate", type=int, required=True)
            s.add_argument("--apply", action="store_true")
            s.add_argument("--i-have-a-pg-dump", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dump with key material
    except (ValueError, OSError):
        pass
    try:
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
        return rewrap(args)
    except SystemExit as exc:  # argparse (usage errors carry no key material)
        return int(exc.code or 0)
    except Refusal as exc:
        sys.stderr.write(f"key_recovery: REFUSED — {exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("key_recovery: interrupted — nothing changed\n")
        return 130
    except BaseException as exc:  # noqa: BLE001 - never a traceback or a message
        sys.stderr.write(f"key_recovery: error ({type(exc).__name__}) — nothing changed\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
