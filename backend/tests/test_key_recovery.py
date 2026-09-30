"""scripts/azure/key_recovery.{sh,py} (D-2026-09-29-2, D-2026-09-30-2) with FAKE keys only.

Hard requirements proven here: the candidate keys (and the current key, the wrapped keys and
the data keys) never appear — as text, as raw bytes, or base64/url-safe base64/hex encoded —
in stdout, stderr or any file written during a run under the test's $HOME, /tmp, /var/tmp,
/dev/shm or the repository — also with a malformed or non-UTF-8 candidate and when the
database or the tunnel fails; the process is non-dumpable; the candidate file must be a
private regular file (0600 in a 0700 directory, owned by the user, no symlink, not in a git
work tree, not on a Windows drive); `shred` overwrites before deleting; `rewrap` changes
exactly one row, only with --apply and --i-have-a-pg-dump, and never in a read-only session;
`erase` is local-only, needs the expected total and a snapshot, refuses an account any key
can unwrap and uses the app's erasure path; an error after COMMIT reports "state unknown".

Every run uses `--candidates` in a temporary directory (never the default location).
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import datetime as dt
import importlib.util
import os
import shutil
import socket
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
CAND_DIR = "kr-test"  # the candidate directory inside the test's $HOME

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


def _encodings(value: str) -> set[bytes]:
    """A key's text, its fragments, its raw bytes and their base64 / url-safe / hex forms."""
    forms = {value.encode(), value[:16].encode(), value[-20:].encode()}
    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except (binascii.Error, ValueError):
        return forms
    if len(raw) < 16:
        return forms
    std = base64.b64encode(raw)
    hexed = raw.hex().encode()
    forms |= {raw, raw[:16], raw[-16:], std, std[:16], std[-20:]}
    forms |= {base64.urlsafe_b64encode(raw)[:16], hexed, hexed.upper(), hexed[:32], hexed[-32:]}
    return forms


class Secrets:
    """Every key of a test, to scan for afterwards."""

    def __init__(self) -> None:
        self.values: list[str] = []

    def key(self) -> str:
        value = Fernet.generate_key().decode()
        self.values.append(value)
        return value

    def patterns(self) -> set[bytes]:
        out: set[bytes] = set()
        for value in self.values:
            out |= _encodings(value)
        return out


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture()
def secrets() -> Secrets:
    return Secrets()


def _cand_path(home: Path) -> Path:
    return home / CAND_DIR / "candidates"


def _run(home: Path, *args: str, env: dict[str, str] | None = None, **kw):
    base = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    base.update({"HOME": str(home), "TMPDIR": str(home.parent), **PYTHON_ENV})
    if "--candidates" not in args:
        args = ("--candidates", str(_cand_path(home)), *args)
    return subprocess.run(
        ["bash", str(TOOL_SH), *args],
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
        **kw,
    )


def _candidate_file(home: Path, lines: list[str] | bytes) -> Path:
    path = _cand_path(home)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    data = lines if isinstance(lines, bytes) else ("\n".join(lines) + "\n").encode()
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _scan_roots(home: Path | None) -> list[Path]:
    roots = [Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"), REPO_ROOT]
    if home is not None:
        roots.append(home)
    return [r for r in roots if r.is_dir()]


def _scan_for_leaks(
    secrets: Secrets,
    since: float,
    outputs: list[str],
    allowed: set[Path],
    home: Path | None = None,
) -> list[str]:
    """Every encoding of every key in the outputs and in any file (re)written since `since`
    under the test's $HOME, /tmp, /var/tmp, /dev/shm and the repository — except the inputs."""
    patterns = secrets.patterns()
    found = [
        f"output #{i}"
        for i, o in enumerate(outputs)
        if any(p in o.encode("utf-8") for p in patterns)
    ]
    skip_dirs = {".git", "node_modules", ".next"}
    seen: set[Path] = set()
    for root in _scan_roots(home):
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for name in filenames:
                path = Path(dirpath) / name
                if path in allowed or path in seen:
                    continue
                seen.add(path)
                try:
                    info = path.lstat()
                    if not path.is_file() or info.st_mtime < since or info.st_size > 20_000_000:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if any(p in data for p in patterns):
                    found.append(str(path))
    return found


def test_the_leak_scan_itself_finds_a_leak(tmp_path: Path, secrets: Secrets) -> None:
    """The scanner must be able to fail: text, raw bytes, hex and standard base64 of a key
    are found under /tmp, /var/tmp and /dev/shm, and in the outputs."""
    key = secrets.key()
    raw = base64.urlsafe_b64decode(key)
    since = time.time() - 1
    leaked = tmp_path / "leak.txt"
    leaked.write_text(f"oops {key[:16]}\n")
    assert _scan_for_leaks(secrets, since, ["clean"], set(), tmp_path) == [str(leaked)]
    assert set(_scan_for_leaks(secrets, since, [f"x{key}x"], {leaked})) == {"output #0"}
    assert _scan_for_leaks(secrets, since, [f"hex {raw.hex()}"], {leaked}) == ["output #0"]
    probes: list[Path] = []
    try:
        for root, data in (
            (Path("/var/tmp"), b"raw " + raw),
            (Path("/dev/shm"), raw.hex().upper().encode()),
            (tmp_path, base64.b64encode(raw)),
        ):
            if not root.is_dir() or not os.access(root, os.W_OK):
                continue
            probe = root / f"kr-scan-probe-{uuid.uuid4().hex}"
            probe.write_bytes(data)
            probes.append(probe)
        found = set(_scan_for_leaks(secrets, since, [], {leaked}, tmp_path))
        assert found == {str(p) for p in probes} and probes
    finally:
        for probe in probes:
            probe.unlink(missing_ok=True)


# --- process hardening (DA-E-2) ---------------------------------------------------------------


def test_harden_makes_the_process_non_dumpable() -> None:
    """In a child interpreter: after _harden(), PR_GET_DUMPABLE is 0 and RLIMIT_CORE 0."""
    code = (
        "import ctypes, importlib.util, resource, sys\n"
        f"spec = importlib.util.spec_from_file_location('kr', {str(TOOL_PY)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "libc = ctypes.CDLL(None)\n"
        "before = libc.prctl(3, 0, 0, 0, 0)\n"
        "m._harden()\n"
        "print(before, libc.prctl(3, 0, 0, 0, 0), resource.getrlimit(resource.RLIMIT_CORE))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["1", "0", "(0,", "0)"]
    assert "PR_SET_DUMPABLE" in TOOL_PY.read_text(encoding="utf-8")
    assert ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) == 1  # this test process is untouched


def test_the_running_tool_is_non_dumpable(home: Path) -> None:
    """Black box: while the tool waits for a silent database, its /proc entry belongs to root
    (the kernel's sign of a non-dumpable process) and its core limit is 0."""
    _candidate_file(home, [Fernet.generate_key().decode()])
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)  # the handshake completes in the backlog; nothing ever answers
    port = silent.getsockname()[1]
    base = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env = {
        **base,
        "HOME": str(home),
        **PYTHON_ENV,
        "PGHOST": "127.0.0.1",
        "PGPORT": str(port),
        "PGUSER": "x",
        "PGDATABASE": "x",
        "PGPASSWORD": "x",
    }
    proc = subprocess.Popen(
        ["bash", str(TOOL_SH), "--candidates", str(_cand_path(home)), "check", "--accounts", "all"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        owner, limits = None, ""
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
                if b"key_recovery.py" in cmdline:
                    limits = Path(f"/proc/{proc.pid}/limits").read_text()
                    owner = os.stat(f"/proc/{proc.pid}/status").st_uid
                    if owner == 0:
                        break
            except OSError:
                pass
            time.sleep(0.05)
        assert owner == 0, "the tool's /proc entry is not root-owned: still dumpable"
        core = next(line for line in limits.splitlines() if line.startswith("Max core file size"))
        assert core.split()[4:6] == ["0", "0"]
    finally:
        proc.kill()
        proc.wait(timeout=10)
        silent.close()


# --- candidate file rules -------------------------------------------------------------------


def test_the_default_candidate_location_is_private_to_home() -> None:
    module = _module()
    assert module.DEFAULT_FILE == Path.home() / ".secrag-recovery" / "candidates"


def test_init_creates_a_private_empty_file_and_never_truncates(home: Path) -> None:
    proc = _run(home, "init")
    assert proc.returncode == 0, proc.stderr
    path = _cand_path(home)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
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
    (home / CAND_DIR).symlink_to(real)
    proc = _run(home, "check", "--accounts", "all")
    assert proc.returncode == 2 and "symlink" in proc.stderr
    (home / CAND_DIR).unlink()
    directory = home / CAND_DIR
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
    path = _cand_path(home)
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


# --- candidate parsing (DA-E-3) ---------------------------------------------------------------


def test_a_bom_and_crlf_file_from_a_windows_editor_still_matches(home: Path) -> None:
    module = _module()
    first, second = Fernet.generate_key(), Fernet.generate_key()
    token = Fernet(first).encrypt(b"data key")
    path = _candidate_file(
        home, b"\xef\xbb\xbf" + first + b"\r\n# note\r\nDATA_MASTER_KEY=" + second + b"\r\n"
    )
    candidates = module.load_candidates(path)
    assert len(candidates) == 2 and None not in candidates
    assert candidates[0].decrypt(token) == b"data key"
    assert candidates[1].decrypt(Fernet(second).encrypt(b"x")) == b"x"


def test_a_non_utf8_line_is_unreadable_and_the_others_still_run(
    home: Path, secrets: Secrets
) -> None:
    good1, good2 = secrets.key(), secrets.key()
    bad = b"\xff\xfe" + secrets.key().encode()[:20] + b"\x80"
    path = _candidate_file(home, good1.encode() + b"\n" + bad + b"\n" + good2.encode() + b"\n")
    since = time.time() - 1
    proc = _run(home, "check", "--accounts", "all", env={"PGHOST": "127.0.0.1", "PGPORT": "1"})
    assert "candidate #2: unreadable (not UTF-8) (skipped)" in proc.stdout
    assert "candidate #1" not in proc.stdout and "candidate #3" not in proc.stdout
    assert proc.returncode == 1 and "error (OperationalError) — nothing changed" in proc.stderr
    assert "UnicodeDecodeError" not in proc.stderr
    module = _module()
    loaded = module.load_candidates(path)
    assert [c is None for c in loaded] == [False, True, False]
    assert _scan_for_leaks(secrets, since, [proc.stdout, proc.stderr], {path}, home) == []


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
    assert _scan_for_leaks(secrets, since, [proc.stdout, proc.stderr], {path}, home) == []


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
    tool = [str(TOOL_SH), "--candidates", str(path), "check", "--accounts", "1"]
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
    assert _scan_for_leaks(secrets, since, [proc.stdout, proc.stderr], {path}, home) == []
    # erase never runs through the tunnel (Azure: the owner deletes the account in the app)
    env_file = tmp_path / "current.env"
    env_file.write_text(f"DATA_MASTER_KEY={secrets.key()}\n")
    env_file.chmod(0o600)
    erase = [str(TOOL_SH), "--candidates", str(path), "erase", "--account", "1"]
    erase += ["--expect-total", "1", "--current-key-from-env-file", str(env_file)]
    refused = subprocess.run(
        ["bash", str(TUNNEL), "--password-from-app", "--read-write", "--", *erase],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert refused.returncode != 0 and "erase is local-only" in refused.stderr
    assert '"rules": []' in state.read_text()
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


def test_erase_refuses_non_local_targets_and_bad_candidates(home: Path, tmp_path: Path) -> None:
    env_file = tmp_path / "current.env"
    env_file.write_text(f"DATA_MASTER_KEY={Fernet.generate_key().decode()}\n")
    env_file.chmod(0o600)
    _candidate_file(home, [Fernet.generate_key().decode()])
    cmd = ["erase", "--account", "1", "--expect-total", "3"]
    cmd += ["--current-key-from-env-file", str(env_file)]
    for env, expected in (
        ({"PGHOST": "db.example.test"}, "PGHOST must be 127.0.0.1"),
        ({"PGHOST": "127.0.0.1,10.0.0.5"}, "PGHOST must be 127.0.0.1"),
        ({"PGHOSTADDR": "192.0.2.10"}, "PGHOSTADDR must be"),
        ({"PGSERVICE": "prod"}, "unset PGSERVICE"),
        ({"PGHOST": "127.0.0.1", "PGAPPNAME": "secrag-db-tunnel"}, "inside db-tunnel.sh"),
    ):
        proc = _run(home, *cmd, env=env)
        assert proc.returncode == 2 and expected in proc.stderr, (env, proc.stderr)
    no_snapshot = _run(home, *cmd, "--apply", env={"PGHOST": "127.0.0.1"})
    assert no_snapshot.returncode == 2 and "--i-have-a-snapshot" in no_snapshot.stderr
    no_total = _run(home, "erase", "--account", "1", "--current-key-from-env-file", str(env_file))
    assert no_total.returncode == 2 and "--expect-total" in no_total.stderr
    _candidate_file(home, [Fernet.generate_key().decode(), "garbage"])
    bad = _run(home, *cmd, env={"PGHOST": "127.0.0.1", "PGPORT": "1"})
    assert bad.returncode == 2 and "candidate(s) #2 could not be read" in bad.stderr


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


# --- database: check, rewrap and erase on a throwaway harness database ------------------------


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


def _seed(url: URL, masters: list[str]) -> tuple[list[bytes], list[uuid.UUID]]:
    """One account per master key, in this order (created_at ascending), each with one
    conversation; returns the data keys and the user ids."""
    engine = create_engine(url, future=True)
    data_keys, ids = [], []
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    try:
        with engine.begin() as conn:
            for i, master in enumerate(masters):
                user_id = uuid.uuid4()
                data_key = Fernet.generate_key()
                data_keys.append(data_key)
                ids.append(user_id)
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
                conn.execute(
                    text("INSERT INTO conversations (id, user_id) VALUES (:c, :id)"),
                    {"c": uuid.uuid4(), "id": user_id},
                )
    finally:
        engine.dispose()
    return data_keys, ids


def _query(url: URL, sql: str, **params) -> list:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql), params)]
    finally:
        engine.dispose()


def _wrapped(url: URL) -> list[bytes]:
    return [
        bytes(r[0])
        for r in _query(
            url,
            "SELECT uk.wrapped_key FROM user_keys uk JOIN users u ON u.id = uk.user_id"
            " ORDER BY u.created_at, u.id",
        )
    ]


@pytest.mark.db
def test_check_and_rewrap_end_to_end(
    recovery_db: URL, home: Path, tmp_path: Path, secrets: Secrets
) -> None:
    old1, current, old2, wrong = (secrets.key() for _ in range(4))
    data_keys, _ids = _seed(recovery_db, [old1, current, old2])
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
    assert "accounts: 3 in total (#1…#3 by creation time, then id)" in lines
    assert "account #1: current key KO" in lines
    assert "account #1: candidate #1 KO" in lines and "account #1: candidate #4 OK" in lines
    assert "account #2: current key OK" in lines
    assert not any(line.startswith("account #2: candidate") for line in lines)
    assert "account #3: candidate #3 OK" in lines
    assert lines[-1].startswith("summary: 3 account(s) checked of 3; 1 readable")

    only = _run(home, "check", "--accounts", "3", "--expect-total", "3", env=pg)
    outputs += [only.stdout, only.stderr]
    assert only.returncode == 0, only.stderr
    labels = {line.split(":")[0] for line in only.stdout.splitlines() if line.startswith("acc")}
    assert labels == {"accounts", "account #3"}  # only the needed account is fetched
    shifted = _run(home, "check", "--accounts", "3", "--expect-total", "4", env=pg)
    assert shifted.returncode == 2 and "--expect-total says 4" in shifted.stderr
    assert "account #3:" not in shifted.stdout

    before = _wrapped(recovery_db)
    read_only_pg = {**pg, "PGOPTIONS": "-c default_transaction_read_only=on"}  # tunnel default
    dry = _run(home, "rewrap", "--account", "1", "--candidate", "4", *cur, env=read_only_pg)
    outputs += [dry.stdout, dry.stderr]
    assert dry.returncode == 0 and "dry run: would update 1 row (account #1)" in dry.stdout
    no_dump = _run(home, "rewrap", "--account", "1", "--candidate", "4", *cur, "--apply", env=pg)
    assert no_dump.returncode == 2 and "--i-have-a-pg-dump" in no_dump.stderr
    apply = ("--apply", "--i-have-a-pg-dump")
    wrong_total = _run(
        home, "rewrap", "--account", "1", "--candidate", "4", "--expect-total", "2", *cur, *apply,
        env=pg,
    )  # fmt: skip
    assert wrong_total.returncode == 2 and "labels may have shifted" in wrong_total.stderr
    wrong_cand = _run(home, "rewrap", "--account", "1", "--candidate", "1", *cur, *apply, env=pg)
    outputs += [wrong_cand.stdout, wrong_cand.stderr]
    assert wrong_cand.returncode == 1 and "candidate #1 KO — nothing changed" in wrong_cand.stdout
    read_only = _run(
        home, "rewrap", "--account", "1", "--candidate", "4", *cur, *apply, env=read_only_pg
    )
    outputs += [read_only.stdout, read_only.stderr]
    assert read_only.returncode == 1
    assert "error (ReadOnlySqlTransaction) — nothing changed" in read_only.stderr
    assert _wrapped(recovery_db) == before  # nothing changed so far

    applied = _run(
        home, "rewrap", "--account", "1", "--candidate", "4", "--expect-total", "3", *cur, *apply,
        env=pg,
    )  # fmt: skip
    outputs += [applied.stdout, applied.stderr]
    assert applied.returncode == 0, applied.stderr
    assert "account #1: re-wrapped under the current master key (1 row updated)" in applied.stdout
    after = _wrapped(recovery_db)
    assert Fernet(current.encode()).decrypt(after[0]) == data_keys[0]  # same data key
    assert after[1:] == before[1:]  # no other row touched
    again = _run(home, "rewrap", "--account", "1", "--candidate", "4", *cur, env=pg)
    assert "already readable with the current key" in again.stdout

    assert _scan_for_leaks(secrets, since, outputs, allowed, home) == []


class _FaultAfterExit:
    """A psycopg connection whose `with` exit (after the COMMIT) raises: the fault a dropped
    connection causes once COMMIT was sent."""

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        import psycopg

        self._real.__exit__(*exc)
        raise psycopg.OperationalError("injected after commit")


@pytest.mark.db
def test_a_fault_after_commit_reports_state_unknown(
    recovery_db: URL,
    home: Path,
    tmp_path: Path,
    secrets: Secrets,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DA-E-4: rewrap --apply and erase --apply with a fault injected after COMMIT print
    "state unknown — run check again", never "nothing changed" — and the change did land."""
    old, current, lost = secrets.key(), secrets.key(), secrets.key()
    data_keys, ids = _seed(recovery_db, [old, current, lost])
    cand = _candidate_file(home, [old])
    env_file = tmp_path / "current.env"
    env_file.write_text(f"DATA_MASTER_KEY={current}\n")
    env_file.chmod(0o600)
    for name, value in _pg_env(recovery_db).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PGAPPNAME", raising=False)
    module = _module()
    monkeypatch.setattr(module, "_harden", lambda: None)  # keep the pytest process dumpable
    real_connect = module._connect
    monkeypatch.setattr(module, "_connect", lambda: _FaultAfterExit(real_connect()))
    cur = ["--current-key-from-env-file", str(env_file)]

    rc = module.main(
        ["--candidates", str(cand), "rewrap", "--account", "1", "--candidate", "1", *cur,
         "--apply", "--i-have-a-pg-dump"]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert rc == 1
    assert "error (OperationalError) after COMMIT was sent — state unknown" in captured.err
    assert "run check again" in captured.err and "nothing changed" not in captured.err
    assert Fernet(current.encode()).decrypt(_wrapped(recovery_db)[0]) == data_keys[0]

    monkeypatch.setattr(module, "_connect", real_connect)
    erase_user, model = module._app_erasure()

    def faulty_erase(session, user_id) -> None:
        import psycopg

        erase_user(session, user_id)
        raise psycopg.OperationalError("injected after commit")

    monkeypatch.setattr(module, "_app_erasure", lambda: (faulty_erase, model))
    rc = module.main(
        ["--candidates", str(cand), "erase", "--account", "3", "--expect-total", "3", *cur,
         "--apply", "--i-have-a-snapshot"]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert rc == 1 and "state unknown" in captured.err and "nothing changed" not in captured.err
    assert _query(recovery_db, "SELECT count(*) FROM users WHERE id = :i", i=ids[2]) == [(0,)]

    # a failure BEFORE the commit is still "nothing changed"
    monkeypatch.setattr(module, "_app_erasure", lambda: (erase_user, model))
    rc = module.main(
        ["--candidates", str(cand), "erase", "--account", "9", "--expect-total", "2", *cur,
         "--apply", "--i-have-a-snapshot"]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert rc == 2 and "account #9 does not exist" in captured.err
    for output in (captured.out, captured.err):
        assert not any(p in output.encode() for p in secrets.patterns())


@pytest.mark.db
def test_erase_end_to_end_through_the_app_erasure_path(
    recovery_db: URL, home: Path, tmp_path: Path, secrets: Secrets
) -> None:
    """D-2026-09-30-2 / DA-E-9: erase refuses a wrong total, a readable account and an account
    a candidate unwraps; a dry run changes nothing; --apply erases exactly the unrecoverable
    account with the app's tombstone; a pre-0005 schema is refused."""
    current, lost, old = secrets.key(), secrets.key(), secrets.key()
    data_keys, ids = _seed(recovery_db, [current, lost, old])
    for key in data_keys:
        secrets.values.append(key.decode())
    cand = _candidate_file(home, [secrets.key(), old])
    env_file = tmp_path / "current.env"
    env_file.write_text(f"DATA_MASTER_KEY={current}\n")
    env_file.chmod(0o600)
    pg = _pg_env(recovery_db)
    cur = ("--current-key-from-env-file", str(env_file))
    snap = ("--apply", "--i-have-a-snapshot")
    since = time.time() - 1
    outputs: list[str] = []
    before = _wrapped(recovery_db)

    def erase(account: str, total: str, *extra: str):
        proc = _run(home, "erase", "--account", account, "--expect-total", total, *cur, *extra,
                    env=pg)  # fmt: skip
        outputs.extend([proc.stdout, proc.stderr])
        return proc

    shifted = erase("2", "4", *snap)
    assert shifted.returncode == 2 and "labels may have shifted" in shifted.stderr
    readable = erase("1", "3", *snap)
    assert readable.returncode == 2 and "readable with the current key" in readable.stderr
    recoverable = erase("3", "3", *snap)
    assert recoverable.returncode == 2
    assert "candidate #2 unwraps account #3 — recover it with rewrap" in recoverable.stderr
    missing = erase("7", "3")
    assert missing.returncode == 2 and "account #7 does not exist" in missing.stderr
    dry = erase("2", "3")
    assert dry.returncode == 0, dry.stderr
    assert "account #2: current key KO, every candidate (2) KO" in dry.stdout
    assert "dry run: would erase account #2" in dry.stdout
    assert _wrapped(recovery_db) == before
    assert _query(recovery_db, "SELECT count(*) FROM deletion_requests") == [(0,)]

    applied = erase("2", "3", *snap)
    assert applied.returncode == 0, applied.stderr
    assert "account #2: erased through the app's erasure path (tombstone written);" in (
        applied.stdout
    )
    assert "2 account(s) remain" in applied.stdout
    assert _query(recovery_db, "SELECT id FROM users ORDER BY created_at") == [
        (ids[0],),
        (ids[2],),
    ]
    assert _query(
        recovery_db, "SELECT user_id, status, completed_at IS NOT NULL FROM deletion_requests"
    ) == [(ids[1], "done", True)]
    assert _query(recovery_db, "SELECT count(*) FROM user_keys WHERE user_id = :i", i=ids[1]) == [
        (0,)
    ]
    assert _query(recovery_db, "SELECT user_id FROM conversations ORDER BY user_id") == sorted(
        [(ids[0],), (ids[2],)]
    )
    assert _wrapped(recovery_db) == [before[0], before[2]]  # other keys untouched
    relabelled = erase("2", "3", *snap)  # the old "#3" is now #2, and the total changed
    assert relabelled.returncode == 2 and "labels may have shifted" in relabelled.stderr

    engine = create_engine(recovery_db, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE deletion_requests DROP COLUMN progress"))
    finally:
        engine.dispose()
    old_schema = erase("2", "2", *snap)
    assert old_schema.returncode == 2 and "migration 0005" in old_schema.stderr
    assert _scan_for_leaks(secrets, since, outputs, {cand, env_file}, home) == []


@pytest.mark.db
def test_erase_refuses_a_current_key_that_is_not_the_apps_key(
    recovery_db: URL, home: Path, tmp_path: Path, secrets: Secrets
) -> None:
    """DA-E2-1: with a wrong/stale env file every account is "current key KO"; erase must
    refuse unless the current key unwraps another account and matches a stored fingerprint.
    Also --no-candidates (D-2026-09-29-2 CHANGED: no candidate file exists)."""
    from rag_app.keycheck import FINGERPRINT_ALGORITHM, fingerprint

    current, lost, other = secrets.key(), secrets.key(), secrets.key()
    data_keys, ids = _seed(recovery_db, [current, lost, current])
    for key in data_keys:
        secrets.values.append(key.decode())
    pg = _pg_env(recovery_db)
    snap = ("--apply", "--i-have-a-snapshot")
    since = time.time() - 1
    outputs: list[str] = []

    def env_file_for(key: str, name: str) -> Path:
        path = tmp_path / name
        path.write_text(f"DATA_MASTER_KEY={key}\n")
        path.chmod(0o600)
        return path

    right = env_file_for(current, "right.env")
    random_key = env_file_for(secrets.key(), "random.env")

    def erase(env_file: Path, *extra: str):
        proc = _run(home, "erase", "--account", "2", "--expect-total", "3", "--no-candidates",
                    "--current-key-from-env-file", str(env_file), *extra, env=pg)  # fmt: skip
        outputs.extend([proc.stdout, proc.stderr])
        return proc

    def unchanged() -> None:
        assert _query(recovery_db, "SELECT count(*) FROM users") == [(3,)]
        assert _query(recovery_db, "SELECT count(*) FROM deletion_requests") == [(0,)]

    # a random "current" key: every account is KO for it → refused, nothing erased
    wrong = erase(random_key, *snap)
    assert wrong.returncode == 2, wrong.stderr
    assert "unwraps none of the 2 other account(s)" in wrong.stderr
    assert "erased" not in wrong.stdout
    unchanged()
    assert not _cand_path(home).exists()  # --no-candidates never needs the file

    def set_fingerprint(key: str) -> None:
        engine = create_engine(recovery_db, future=True)
        try:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM master_key_fingerprint"))
                conn.execute(
                    text(
                        "INSERT INTO master_key_fingerprint (id, fingerprint, algorithm)"
                        " VALUES (1, :fp, :alg)"
                    ),
                    {"fp": fingerprint(key), "alg": FINGERPRINT_ALGORITHM},
                )
        finally:
            engine.dispose()

    # a stored fingerprint of another key → refused even though the key unwraps others
    set_fingerprint(other)
    mismatch = erase(right, *snap)
    assert mismatch.returncode == 2
    assert "does not match the master-key fingerprint" in mismatch.stderr
    unchanged()

    set_fingerprint(current)
    dry = erase(right)
    assert dry.returncode == 0, dry.stderr
    assert "candidates: none (--no-candidates: key recovery skipped)" in dry.stdout
    assert "current key verified: unwraps 2 of 2 other account(s); fingerprint matches" in (
        dry.stdout
    )
    assert "account #2: current key KO, no candidates tried (--no-candidates)" in dry.stdout
    unchanged()

    applied = erase(right, *snap)
    assert applied.returncode == 0, applied.stderr
    assert "account #2: erased through the app's erasure path" in applied.stdout
    assert _query(recovery_db, "SELECT user_id, status FROM deletion_requests") == [
        (ids[1], "done")
    ]
    assert _query(recovery_db, "SELECT count(*) FROM users") == [(2,)]
    after = _run(home, "check", "--accounts", "all", "--expect-total", "2", "--no-candidates",
                 "--current-key-from-env-file", str(right), env=pg)  # fmt: skip
    outputs.extend([after.stdout, after.stderr])
    assert after.returncode == 0, after.stderr
    assert "summary: 2 account(s) checked of 2; 2 readable with the current key" in after.stdout
    assert _scan_for_leaks(secrets, since, outputs, {right, random_key}, home) == []
