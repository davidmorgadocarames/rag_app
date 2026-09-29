"""scripts/azure/key_recovery.{sh,py} (D-2026-09-29-2) with FAKE keys only.

Hard requirements proven here: the candidate keys (and the current key, the wrapped keys and
the data keys) never appear in stdout, stderr or any file written during a run under $HOME,
/tmp or the repository — also with a malformed candidate and when the database or the
tunnel fails; the candidate file must be a private regular file (0600 in a 0700 directory,
owned by the user, no symlink, not in a git work tree, not on a Windows drive); `shred`
overwrites before deleting; `rewrap` changes exactly one row, only with --apply and
--i-have-a-pg-dump, and never in a read-only session.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SH = REPO_ROOT / "scripts" / "azure" / "key_recovery.sh"
TOOL_PY = REPO_ROOT / "scripts" / "azure" / "key_recovery.py"
TUNNEL = REPO_ROOT / "scripts" / "azure" / "db-tunnel.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
# The interpreter running the tests has the tool's dependencies (the pre-push worktree and CI
# have no backend/.venv of their own).
PYTHON_ENV = {"KEY_RECOVERY_PYTHON": sys.executable}


def _module():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # importing it here must not leave a __pycache__ either
    try:
        spec = importlib.util.spec_from_file_location("key_recovery", TOOL_PY)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


class Secrets:
    """Every key of a test, to scan for afterwards."""

    def __init__(self) -> None:
        self.values: list[str] = []

    def key(self) -> str:
        value = Fernet.generate_key().decode()
        self.values.append(value)
        return value

    def fragments(self) -> set[str]:
        out: set[str] = set()
        for value in self.values:
            out.add(value)
            out.add(value[:16])
            out.add(value[-20:])
        return out


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture()
def secrets() -> Secrets:
    return Secrets()


def _run(home: Path, *args: str, env: dict[str, str] | None = None, **kw):
    base = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    base.update({"HOME": str(home), "TMPDIR": str(home.parent), **PYTHON_ENV})
    return subprocess.run(
        ["bash", str(TOOL_SH), *args],
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
        **kw,
    )


def _candidate_file(home: Path, lines: list[str]) -> Path:
    directory = home / ".secrag-recovery"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "candidates"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _scan_for_leaks(
    secrets: Secrets, since: float, outputs: list[str], allowed: set[Path]
) -> list[str]:
    """Every fragment of every key in the outputs and in any file (re)written since `since`
    under $HOME (the test's), /tmp and the repository — except the input files."""
    found = [f"output #{i}" for i, o in enumerate(outputs) for s in secrets.fragments() if s in o]
    roots = [Path("/tmp"), REPO_ROOT]
    skip_dirs = {".git", "node_modules", ".next"}
    fragments = [s.encode() for s in secrets.fragments()]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for name in filenames:
                path = Path(dirpath) / name
                if path in allowed:
                    continue
                try:
                    info = path.lstat()
                    if not path.is_file() or info.st_mtime < since or info.st_size > 20_000_000:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                found += [f"{path}" for frag in fragments if frag in data]
    return found


def test_the_leak_scan_itself_finds_a_leak(tmp_path: Path, secrets: Secrets) -> None:
    """The scanner must be able to fail: a key written to a file under /tmp is found."""
    key = secrets.key()
    since = time.time() - 1
    leaked = tmp_path / "leak.txt"
    leaked.write_text(f"oops {key[:16]}\n")
    assert _scan_for_leaks(secrets, since, ["clean"], set()) == [str(leaked)]
    assert set(_scan_for_leaks(secrets, since, [f"x{key}x"], {leaked})) == {"output #0"}


# --- candidate file rules -------------------------------------------------------------------


def test_init_creates_a_private_empty_file_and_never_truncates(home: Path) -> None:
    proc = _run(home, "init")
    assert proc.returncode == 0, proc.stderr
    directory, path = home / ".secrag-recovery", home / ".secrag-recovery" / "candidates"
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text() == ""
    assert "nano" in proc.stdout and "Ctrl-D" in proc.stdout and "cat >" in proc.stdout
    path.write_text("# kept\n")
    assert _run(home, "init").returncode == 0
    assert path.read_text() == "# kept\n"  # an existing file is never truncated


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda p: p.chmod(0o644), "mode 644"),
        (lambda p: p.chmod(0o640), "mode 640"),
        (lambda p: p.parent.chmod(0o755), "directory has mode 755"),
    ],
    ids=["file-0644", "file-0640", "dir-0755"],
)
def test_wider_permissions_are_refused(home: Path, setup, expected: str) -> None:
    path = _candidate_file(home, [Fernet.generate_key().decode()])
    setup(path)
    proc = _run(home, "check", "--accounts", "all")
    assert proc.returncode == 2 and "REFUSED" in proc.stderr and expected in proc.stderr


def test_a_symlinked_file_or_directory_is_refused(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    (real / "candidates").write_text("x\n")
    (real / "candidates").chmod(0o600)
    (home / ".secrag-recovery").symlink_to(real)
    proc = _run(home, "check", "--accounts", "all")
    assert proc.returncode == 2 and "symlink" in proc.stderr
    (home / ".secrag-recovery").unlink()
    directory = home / ".secrag-recovery"
    directory.mkdir(mode=0o700)
    (directory / "candidates").symlink_to(real / "candidates")
    proc = _run(home, "check", "--accounts", "all")
    assert proc.returncode == 2 and "symlink" in proc.stderr


def test_a_file_inside_a_git_work_tree_is_refused(home: Path) -> None:
    (home / ".git").mkdir()  # e.g. a dotfiles repository in $HOME
    _candidate_file(home, [Fernet.generate_key().decode()])
    proc = _run(home, "check", "--accounts", "all")
    assert proc.returncode == 2 and "git work tree" in proc.stderr
    proc = _run(home, "--candidates", str(REPO_ROOT / "candidates"), "init")
    assert proc.returncode == 2 and "git work tree" in proc.stderr
    assert not (REPO_ROOT / "candidates").exists()


def test_windows_drives_are_refused(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _run(home, "--candidates", "/mnt/c/secrag-never-created/candidates", "init")
    assert proc.returncode == 2 and "Windows drive" in proc.stderr
    module = _module()
    path = home / ".secrag-recovery" / "candidates"
    monkeypatch.setattr(module, "_mounts", lambda: [("/", "ext4"), (str(home), "9p")])
    with pytest.raises(module.Refusal, match="Windows drive"):
        module._check_location(path)


def test_the_gitignore_ignores_recovery_files_defensively() -> None:
    for probe in ("x/.secrag-recovery/candidates", "candidates", "a/b/candidates"):
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", probe],
            capture_output=True,
        )
        assert proc.returncode == 0, probe


def test_gitleaks_allowlist_is_not_widened_for_recovery() -> None:
    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "recovery" not in config and "candidates" not in config


# --- no leak: malformed candidates, database failure, tunnel failure ---------------------------


def test_malformed_candidates_are_reported_by_index_only(home: Path, secrets: Secrets) -> None:
    good = secrets.key()
    bad = "not-a-key-" + secrets.key()[:30]  # key-like, but invalid
    secrets.values.append(bad)
    path = _candidate_file(
        home, ["# comment", "", f"DATA_MASTER_KEY={good}", bad, f"'{secrets.key()}'"]
    )
    since = time.time() - 1
    proc = _run(home, "check", "--accounts", "all", env={"PGHOST": "127.0.0.1", "PGPORT": "1"})
    assert "candidate #2: not a valid Fernet key (skipped)" in proc.stdout
    assert proc.returncode == 1 and "error (OperationalError)" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert _scan_for_leaks(secrets, since, [proc.stdout, proc.stderr], {path}) == []


def test_a_tunnel_failure_leaks_nothing(home: Path, tmp_path: Path, secrets: Secrets) -> None:
    """The tool as the tunnel's command: the fake tunnel cannot reach a server (connection
    refused on 127.0.0.2); only the class name is printed and the rule is removed."""
    path = _candidate_file(home, [secrets.key(), secrets.key()])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    from test_db_tunnel import FAKE_AZ, FAKE_CURL  # the tunnel's own fakes

    (bin_dir / "az.py").write_text(FAKE_AZ, encoding="utf-8")
    (bin_dir / "az").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir}/az.py" "$@"\n')
    (bin_dir / "az").chmod(0o755)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    state = tmp_path / "az_state.json"
    state.write_text('{"rules": [], "host": "127.0.0.2"}')
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PG")},
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        **PYTHON_ENV,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AZ_STATE": str(state),
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
        "FAKE_IP_A": "203.0.113.7",
        "FAKE_IP_B": "203.0.113.7",
        "FAKE_DB_URL": "postgresql://secragadmin:pw@127.0.0.2/rag",
    }
    since = time.time() - 1
    tool = [str(TOOL_SH), "check", "--accounts", "1"]
    proc = subprocess.run(
        ["bash", str(TUNNEL), "--password-from-app", "--", *tool],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stderr
    assert "error (OperationalError)" in proc.stderr and "removed" in proc.stderr
    assert '"rules": []' in state.read_text()
    assert _scan_for_leaks(secrets, since, [proc.stdout, proc.stderr], {path}) == []
    # only this repository's key_recovery.sh may run through the tunnel
    copy = tmp_path / "key_recovery.sh"
    shutil.copy(TOOL_SH, copy)
    refused = subprocess.run(
        ["bash", str(TUNNEL), "--password-from-app", "--", str(copy), "check", "--accounts", "1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode == 1 and "only this repository" in refused.stderr


def test_shred_overwrites_then_removes_the_file_and_directory(home: Path, tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    path = _candidate_file(home, [key, key])
    size = path.stat().st_size
    observer = tmp_path / "same-inode"
    os.link(path, observer)  # sees the inode's content after the unlink
    proc = _run(home, "shred")
    assert proc.returncode == 0, proc.stderr
    assert not path.exists() and not path.parent.exists()
    assert observer.read_bytes() == b"\0" * size  # random pass, then zeros
    assert key not in proc.stdout + proc.stderr


def test_the_current_key_env_file_must_be_private(home: Path, tmp_path: Path) -> None:
    _candidate_file(home, [Fernet.generate_key().decode()])
    env_file = tmp_path / "app.env"
    env_file.write_text(f"DATA_MASTER_KEY={Fernet.generate_key().decode()}\n")
    env_file.chmod(0o644)
    proc = _run(home, "check", "--accounts", "all", "--current-key-from-env-file", str(env_file))
    assert proc.returncode == 2 and "chmod 600" in proc.stderr


def test_the_tool_leaves_no_bytecode_and_disables_core_dumps() -> None:
    script = TOOL_SH.read_text(encoding="utf-8")
    assert "-I -B" in script and "ulimit -c 0" in script
    assert "RLIMIT_CORE" in TOOL_PY.read_text(encoding="utf-8")
    assert not (TOOL_PY.parent / "__pycache__" / "key_recovery.cpython-312.pyc").exists()


# --- database: check and rewrap on a throwaway harness database -----------------------------


@pytest.fixture()
def recovery_db(admin_url: URL) -> Iterator[URL]:
    from db_harness import apply_roles, create_database, drop_database, migrate

    url = create_database(admin_url)
    engine = create_engine(url, future=True)
    try:
        apply_roles(engine)
    finally:
        engine.dispose()
    migrate(url)
    try:
        yield url
    finally:
        drop_database(admin_url, url)


def _pg_env(url: URL, **extra: str) -> dict[str, str]:
    return {
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        **extra,
    }


def _seed(url: URL, masters: list[str]) -> list[bytes]:
    """One account per master key, in this order (created_at ascending); returns the data
    keys."""
    engine = create_engine(url, future=True)
    data_keys = []
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    try:
        with engine.begin() as conn:
            for i, master in enumerate(masters):
                user_id = uuid.uuid4()
                data_key = Fernet.generate_key()
                data_keys.append(data_key)
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, created_at)"
                        " VALUES (:id, :e, 'h', :t)"
                    ),
                    {
                        "id": user_id,
                        "e": f"{user_id.hex}@example.test",
                        "t": base + dt.timedelta(days=i),
                    },
                )
                conn.execute(
                    text("INSERT INTO user_keys (user_id, wrapped_key) VALUES (:id, :w)"),
                    {"id": user_id, "w": Fernet(master.encode()).encrypt(data_key)},
                )
    finally:
        engine.dispose()
    return data_keys


def _wrapped(url: URL) -> list[bytes]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return [
                bytes(r[0])
                for r in conn.execute(
                    text(
                        "SELECT uk.wrapped_key FROM user_keys uk JOIN users u ON u.id = uk.user_id"
                        " ORDER BY u.created_at, u.id"
                    )
                )
            ]
    finally:
        engine.dispose()


@pytest.mark.db
def test_check_and_rewrap_end_to_end(
    recovery_db: URL, home: Path, tmp_path: Path, secrets: Secrets
) -> None:
    old1, current, old2, wrong = (secrets.key() for _ in range(4))
    data_keys = _seed(recovery_db, [old1, current, old2])
    for key in data_keys:
        secrets.values.append(key.decode())
    cand = _candidate_file(home, [wrong, "garbage-line", old2, old1])
    env_file = tmp_path / "current.env"
    env_file.write_text(f'DATA_MASTER_KEY="{current}"\n')
    env_file.chmod(0o600)
    pg = _pg_env(recovery_db)
    cur = ("--current-key-from-env-file", str(env_file))
    allowed = {cand, env_file}
    since = time.time() - 1
    outputs: list[str] = []

    proc = _run(home, "check", "--accounts", "all", *cur, env=pg)
    outputs += [proc.stdout, proc.stderr]
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "candidate #2: not a valid Fernet key (skipped)" in lines
    assert "account #1: current key KO" in lines
    assert "account #1: candidate #1 KO" in lines and "account #1: candidate #4 OK" in lines
    assert "account #2: current key OK" in lines
    assert not any(line.startswith("account #2: candidate") for line in lines)
    assert "account #3: candidate #3 OK" in lines
    assert lines[-1].startswith("summary: 3 account(s) checked; 1 readable")

    only = _run(home, "check", "--accounts", "3", env=pg)  # only the needed account is fetched
    outputs += [only.stdout, only.stderr]
    labels = {line.split(":")[0] for line in only.stdout.splitlines() if line.startswith("acc")}
    assert labels == {"account #3"}

    before = _wrapped(recovery_db)
    read_only_pg = {**pg, "PGOPTIONS": "-c default_transaction_read_only=on"}  # tunnel default
    dry = _run(home, "rewrap", "--account", "1", "--candidate", "4", *cur, env=read_only_pg)
    outputs += [dry.stdout, dry.stderr]
    assert dry.returncode == 0 and "dry run: would update 1 row (account #1)" in dry.stdout
    no_dump = _run(home, "rewrap", "--account", "1", "--candidate", "4", *cur, "--apply", env=pg)
    assert no_dump.returncode == 2 and "--i-have-a-pg-dump" in no_dump.stderr
    wrong_cand = _run(
        home,
        "rewrap",
        "--account",
        "1",
        "--candidate",
        "1",
        *cur,
        "--apply",
        "--i-have-a-pg-dump",
        env=pg,
    )
    outputs += [wrong_cand.stdout, wrong_cand.stderr]
    assert wrong_cand.returncode == 1 and "candidate #1 KO — nothing changed" in wrong_cand.stdout
    read_only = _run(
        home,
        "rewrap",
        "--account",
        "1",
        "--candidate",
        "4",
        *cur,
        "--apply",
        "--i-have-a-pg-dump",
        env={**pg, "PGOPTIONS": "-c default_transaction_read_only=on"},
    )
    outputs += [read_only.stdout, read_only.stderr]
    assert read_only.returncode == 1 and "error (ReadOnlySqlTransaction)" in read_only.stderr
    assert _wrapped(recovery_db) == before  # nothing changed so far

    applied = _run(
        home,
        "rewrap",
        "--account",
        "1",
        "--candidate",
        "4",
        *cur,
        "--apply",
        "--i-have-a-pg-dump",
        env=pg,
    )
    outputs += [applied.stdout, applied.stderr]
    assert applied.returncode == 0, applied.stderr
    assert "account #1: re-wrapped under the current master key (1 row updated)" in applied.stdout
    after = _wrapped(recovery_db)
    assert Fernet(current.encode()).decrypt(after[0]) == data_keys[0]  # same data key
    assert after[1:] == before[1:]  # no other row touched
    again = _run(home, "rewrap", "--account", "1", "--candidate", "4", *cur, env=pg)
    assert "already readable with the current key" in again.stdout

    assert _scan_for_leaks(secrets, since, outputs, allowed) == []
