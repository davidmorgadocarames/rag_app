"""Backups (T11.2.10, T11.2.13) and the 14-day retention constant (X9).

- X9: one constant (``rag_app.retention.BACKUP_RETENTION_DAYS``) — the shell scripts read it
  from that file, and the docs state the same number.
- ``scripts/db/backup.sh`` file mode with a FAKE ``pg_dump`` and a THROWAWAY age key: only
  ciphertext is written, only the ``secrag_backup`` role and public keys are accepted, files
  older than the retention (by the time in their name) are removed, nothing else is.
- Blob mode with a FAKE Azure SDK (``tests/fakes/azure``): upload of ciphertext only, never an
  overwrite, the oldest-blob check (the real Blob check is row 42).
- ``scripts/db/backup-pull.sh`` with a FAKE ``az``: latest dump + tombstone export, prune.
- ``scripts/db/restore.sh`` refusals that need no database; the tombstone export format.
- DB (harness): export → restored DB without the tombstone → union + replay re-erases.
The end-to-end drill (real pg_dump/pg_restore/age) is the gate step ``backup-drill``.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

from rag_app import backup_blob, tombstones
from rag_app.retention import BACKUP_RETENTION_DAYS

REPO = Path(__file__).resolve().parents[2]
DB_SCRIPTS = REPO / "scripts" / "db"
FAKES = Path(__file__).resolve().parent / "fakes"
AGE = shutil.which("age") and shutil.which("age-keygen")
needs_age = pytest.mark.skipif(not AGE, reason="needs age + age-keygen")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _stamp(days_ago: float) -> str:
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    return f"{moment:%Y%m%dT%H%M%SZ}"


# --- X9: one constant -----------------------------------------------------------------------


def test_the_retention_promise_is_14_days() -> None:
    assert BACKUP_RETENTION_DAYS == 14


@needs_bash
def test_the_scripts_read_the_constant_from_retention_py() -> None:
    proc = subprocess.run(
        ["bash", str(DB_SCRIPTS / "backup.sh"), "--retention-days"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == str(BACKUP_RETENTION_DAYS)
    # No script hard-codes the number: they all read it through backup_lib.sh.
    for path in DB_SCRIPTS.glob("*.sh"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(rf"\b{BACKUP_RETENTION_DAYS}\b", text), path.name


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "docs/adr/adr_phase06_gdpr_erasure.md",
        "docs/adr/adr_phase09_deployment.md",
        "docs/adr/adr_phase11_stability.md",
        "deploy/azure/jobs/backup.yaml",
    ],
)
def test_the_docs_state_the_same_retention(doc: str) -> None:
    text = (REPO / doc).read_text(encoding="utf-8")
    n = BACKUP_RETENTION_DAYS
    assert f"{n} days" in text or f"{n}-day" in text, doc
    assert "7-day rotation" not in text and "at most **7 days**" not in text, doc


# --- helpers ----------------------------------------------------------------------------------


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, str]:
    """A THROWAWAY age keypair (private key 0600 in a temp dir; never printed)."""
    if not AGE:
        pytest.skip("needs age")
    directory = tmp_path / "kr-test-keys"
    directory.mkdir(mode=0o700)
    key = directory / "backup.key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    return key, recipient


def _fake_bin(tmp_path: Path) -> Path:
    """A fake pg_dump that records the libpq role/database it was given; with
    FAKE_PG_DUMP_FAIL set it writes a truncated dump and fails (DA-F-2)."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    pg_dump = bin_dir / "pg_dump"
    pg_dump.write_text(
        '#!/usr/bin/env bash\nprintf "PGDMP fake dump of %s as %s\\n" "$PGDATABASE" "$PGUSER"\n'
        'if [ -n "${FAKE_PG_DUMP_FAIL:-}" ]; then echo "pg_dump: error: connection lost" >&2;'
        " exit 3; fi\n"
    )
    pg_dump.chmod(0o755)
    return bin_dir


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "BACKUP_", "AZURE_"))}
    base["PATH"] = f"{_fake_bin(tmp_path)}:{base['PATH']}"
    base["HOME"] = str(tmp_path / "home")
    base["SECRAG_PYTHON"] = sys.executable
    return {**base, **extra}


def _backup(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DB_SCRIPTS / "backup.sh"), *args],
        env=_env(tmp_path, **env),
        capture_output=True,
        text=True,
        timeout=60,
    )


BACKUP_URL = "postgresql+psycopg://secrag_backup:pw@127.0.0.1:1/secrag"


# --- backup.sh file mode --------------------------------------------------------------------


@needs_age
def test_file_mode_writes_ciphertext_and_prunes_by_name(
    tmp_path: Path, keys: tuple[Path, str]
) -> None:
    key, recipient = keys
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    old = out_dir / f"secrag-{_stamp(BACKUP_RETENTION_DAYS + 0.5)}.dump.age"
    recent = out_dir / f"secrag-{_stamp(BACKUP_RETENTION_DAYS - 0.5)}.dump.age"
    other = out_dir / "notes.txt"
    for path in (old, recent, other):
        path.write_text("age-encryption.org/v1\nplanted\n")
    os.utime(recent, (0, 0))  # an ancient mtime does not matter: the NAME carries the age

    proc = _backup(
        tmp_path, "--file", "--dir", str(out_dir), DATABASE_URL=BACKUP_URL,
        BACKUP_AGE_RECIPIENT=recipient,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert f"removed {old.name} (older than {BACKUP_RETENTION_DAYS} days)" in proc.stdout
    assert not old.exists() and recent.exists() and other.exists()
    new = [p for p in out_dir.glob("secrag-*.dump.age") if p != recent]
    assert len(new) == 1
    assert oct(new[0].stat().st_mode & 0o777) == "0o600"
    assert oct(out_dir.stat().st_mode & 0o777) == "0o700"
    data = new[0].read_bytes()
    assert data.startswith(b"age-encryption.org/v1\n") and b"PGDMP" not in data
    plain = subprocess.run(
        ["age", "-d", "-i", str(key), str(new[0])], check=True, capture_output=True
    ).stdout
    assert plain == b"PGDMP fake dump of secrag as secrag_backup\n"
    assert not list(out_dir.glob(".*partial"))


@needs_age
def test_file_mode_refusals(tmp_path: Path, keys: tuple[Path, str]) -> None:
    key, recipient = keys
    out_dir = tmp_path / "backups"
    owner = _backup(
        tmp_path, "--file", "--dir", str(out_dir),
        DATABASE_URL="postgresql://rag:rag@127.0.0.1:5432/rag", BACKUP_AGE_RECIPIENT=recipient,
    )  # fmt: skip
    assert owner.returncode != 0 and "read-only role secrag_backup" in owner.stderr

    secret_line = next(
        line for line in key.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-")
    )
    private = _backup(
        tmp_path, "--file", "--dir", str(out_dir), DATABASE_URL=BACKUP_URL,
        BACKUP_AGE_RECIPIENT=secret_line,
    )  # fmt: skip
    assert private.returncode != 0 and "never give this script a private key" in private.stderr
    assert secret_line not in private.stdout + private.stderr

    in_repo = _backup(
        tmp_path, "--file", "--dir", str(REPO / "backups-test"), DATABASE_URL=BACKUP_URL,
        BACKUP_AGE_RECIPIENT=recipient,
    )  # fmt: skip
    assert in_repo.returncode != 0 and "inside a git work tree" in in_repo.stderr
    assert not (REPO / "backups-test").exists()

    none = _backup(
        tmp_path, "--file", "--dir", str(out_dir), "--recipient-file", str(tmp_path / "missing"),
        DATABASE_URL=BACKUP_URL,
    )  # fmt: skip
    assert none.returncode != 0 and "no age recipient" in none.stderr
    assert not out_dir.exists() or not list(out_dir.iterdir())


# --- Blob mode (fake Azure SDK) -------------------------------------------------------------


class _MemoryContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, dt.datetime]] = {}

    def upload_blob(self, name: str, data, overwrite: bool = False) -> None:
        if name in self.blobs and not overwrite:
            raise RuntimeError("exists")
        self.blobs[name] = (b"".join(data), dt.datetime.now(dt.UTC))

    def get_blob_client(self, blob: str):
        return types.SimpleNamespace(
            get_blob_properties=lambda: types.SimpleNamespace(size=len(self.blobs[blob][0]))
        )

    def list_blobs(self, name_starts_with: str | None = None):
        for name, (_data, created) in self.blobs.items():
            if name.startswith(name_starts_with or ""):
                yield types.SimpleNamespace(name=name, creation_time=created)


def test_blob_upload_accepts_only_ciphertext_and_never_overwrites() -> None:
    client = _MemoryContainer()
    name = f"backups/secrag-{_stamp(0)}.dump.age"
    body = b"age-encryption.org/v1\n" + b"x" * 100
    assert backup_blob.upload(client, name, io.BytesIO(body)) == len(body)
    assert client.blobs[name][0] == body
    with pytest.raises(backup_blob.BackupError, match="not age-encrypted"):
        backup_blob.upload(client, f"backups/secrag-{_stamp(1)}.dump.age", io.BytesIO(b"PGDMP"))
    with pytest.raises(backup_blob.BackupError, match="blob name"):
        backup_blob.upload(client, "backups/other.bin", io.BytesIO(body))
    with pytest.raises(RuntimeError):
        backup_blob.upload(client, name, io.BytesIO(body))  # never overwrites


def test_the_oldest_blob_check_enforces_the_retention() -> None:
    client = _MemoryContainer()
    with pytest.raises(backup_blob.BackupError, match="no backup blob"):
        backup_blob.check(client)
    now = dt.datetime.now(dt.UTC)
    client.blobs[f"backups/secrag-{_stamp(1)}.dump.age"] = (b"", now - dt.timedelta(days=1))
    client.blobs["tombstones/tombstones-20200101T000000Z.jsonl"] = (b"", now - dt.timedelta(999))
    count, oldest, newest = backup_blob.check(client, now)
    assert (count, round(oldest), round(newest)) == (1, 1, 1)
    client.blobs[f"backups/secrag-{_stamp(15)}.dump.age"] = (
        b"",
        now - dt.timedelta(days=BACKUP_RETENTION_DAYS, hours=1),
    )
    with pytest.raises(backup_blob.BackupError, match="over the 14-day retention promise"):
        backup_blob.check(client, now)


@needs_age
def test_blob_mode_end_to_end_with_the_fake_sdk(tmp_path: Path, keys: tuple[Path, str]) -> None:
    key, recipient = keys
    root = tmp_path / "blob"
    env = {
        "DATABASE_URL": BACKUP_URL,
        "BACKUP_AGE_RECIPIENT": recipient,
        "BACKUP_STORAGE_ACCOUNT": "secragbackups",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
        "FAKE_BLOB_ROOT": str(root),
        "PYTHONPATH": f"{FAKES}:{REPO / 'backend' / 'src'}",
    }
    proc = _backup(tmp_path, "--blob", **env)
    assert proc.returncode == 0, proc.stderr
    blobs = list((root / "secrag-backups" / "backups").glob("secrag-*.dump.age"))
    assert len(blobs) == 1
    assert "backup_blob: 1 backup blob(s); oldest 0.0 days" in proc.stdout
    connection = (root / "_connection").read_text()
    assert connection.startswith("https://secragbackups.blob.core.windows.net ManagedIdentity")
    plain = subprocess.run(
        ["age", "-d", "-i", str(key), str(blobs[0])], check=True, capture_output=True
    ).stdout
    assert plain.startswith(b"PGDMP")
    same_second = _backup(tmp_path, "--blob", **env)  # same name → never overwritten
    if same_second.returncode != 0:
        assert "ResourceExistsError" in same_second.stderr
    for blob in (root / "secrag-backups" / "backups").glob("*"):
        blob.unlink()
    # an old blob the lifecycle rule failed to delete → the Job fails (X9)
    stale = blobs[0].with_name(f"secrag-{_stamp(20)}.dump.age")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"age-encryption.org/v1\n")
    old = (dt.datetime.now() - dt.timedelta(days=20)).timestamp()
    os.utime(stale, (old, old))
    again = _backup(tmp_path, "--blob", **env)
    assert again.returncode != 0 and "over the 14-day retention promise" in again.stderr
    no_identity = _backup(tmp_path, "--blob", **{**env, "AZURE_CLIENT_ID": ""})
    assert no_identity.returncode != 0 and "AZURE_CLIENT_ID" in no_identity.stderr


# --- DA-F-2: a failed pg_dump never leaves a backup behind -----------------------------------


@needs_age
def test_a_failing_pg_dump_keeps_no_file(tmp_path: Path, keys: tuple[Path, str]) -> None:
    _key, recipient = keys
    out_dir = tmp_path / "backups"
    proc = _backup(
        tmp_path, "--file", "--dir", str(out_dir), DATABASE_URL=BACKUP_URL,
        BACKUP_AGE_RECIPIENT=recipient, FAKE_PG_DUMP_FAIL="1",
    )  # fmt: skip
    assert proc.returncode != 0
    assert "pg_dump (rc 3) or age (rc 0) failed — no backup kept or uploaded" in proc.stderr
    assert list(out_dir.iterdir()) == []  # neither a dump nor a .partial


@needs_age
def test_a_failing_pg_dump_uploads_no_blob(tmp_path: Path, keys: tuple[Path, str]) -> None:
    _key, recipient = keys
    root, scratch = tmp_path / "blob", tmp_path / "tmp"
    scratch.mkdir()
    env = {
        "DATABASE_URL": BACKUP_URL,
        "BACKUP_AGE_RECIPIENT": recipient,
        "BACKUP_STORAGE_ACCOUNT": "secragbackups",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
        "FAKE_BLOB_ROOT": str(root),
        "PYTHONPATH": f"{FAKES}:{REPO / 'backend' / 'src'}",
        "TMPDIR": str(scratch),
    }
    failed = _backup(tmp_path, "--blob", **env, FAKE_PG_DUMP_FAIL="1")
    assert failed.returncode != 0
    assert "pg_dump (rc 3) or age (rc 0) failed" in failed.stderr
    assert not root.exists() or not [p for p in root.rglob("*") if p.is_file()]
    assert list(scratch.iterdir()) == []  # the temporary ciphertext is gone too
    ok = _backup(tmp_path, "--blob", **env)  # the same set-up without the fault uploads
    assert ok.returncode == 0, ok.stderr
    assert len(list((root / "secrag-backups" / "backups").glob("secrag-*.dump.age"))) == 1
    assert list(scratch.iterdir()) == []


# --- DA-F-3: retention flags names it cannot judge and sweeps stale partial files ------------


@needs_bash
def test_retention_flags_bad_dates_and_sweeps_stale_partials(tmp_path: Path) -> None:
    folder = tmp_path / "dumps"
    folder.mkdir()
    old = f"secrag-{_stamp(BACKUP_RETENTION_DAYS + 1)}.dump.age"
    fresh = f"secrag-{_stamp(1)}.dump.age"
    invalid = "secrag-20261399T000000Z.dump.age"  # month 13
    feb31 = "secrag-20260231T120000Z.dump.age"  # not a real day
    future = f"secrag-{_stamp(-3)}.dump.age"
    stale_partial = f".secrag-{_stamp(2)}.dump.age.partial"
    new_partial = f".secrag-{_stamp(0)}.dump.age.partial"
    foreign_partial = ".notes.txt.partial"
    other = "secrag-backup-notes.txt"
    names = [old, fresh, invalid, feb31, future, stale_partial, new_partial, foreign_partial]
    for name in [*names, other]:
        (folder / name).write_text("age-encryption.org/v1\n")
    two_days = (dt.datetime.now() - dt.timedelta(days=2)).timestamp()
    for name in (stale_partial, foreign_partial):
        os.utime(folder / name, (two_days, two_days))
    lib = DB_SCRIPTS / "backup_lib.sh"
    proc = subprocess.run(
        ["bash", "-c", f'. "{lib}"; prune_by_name "$1" "$DUMP_NAME_RE" "$2"', "prune",
         str(folder), str(BACKUP_RETENTION_DAYS)],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 1, proc.stderr
    left = {p.name for p in folder.iterdir()}
    assert left == {fresh, invalid, feb31, future, new_partial, foreign_partial, other}
    assert f"removed {old} (older than" in proc.stdout
    assert f"removed {stale_partial} (stale partial file)" in proc.stdout
    for name in (invalid, feb31):
        assert f"WARNING: {name} has an invalid date" in proc.stderr
    assert f"WARNING: {future} is dated in the future" in proc.stderr
    for name in (invalid, feb31, future):
        (folder / name).unlink()
    clean = subprocess.run(
        ["bash", "-c", f'. "{lib}"; prune_by_name "$1" "$DUMP_NAME_RE" "$2"', "prune",
         str(folder), str(BACKUP_RETENTION_DAYS)],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert clean.returncode == 0 and clean.stderr == ""


@needs_age
def test_file_mode_fails_loudly_on_a_dump_name_it_cannot_judge(
    tmp_path: Path, keys: tuple[Path, str]
) -> None:
    _key, recipient = keys
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    (out_dir / "secrag-20261399T000000Z.dump.age").write_text("age-encryption.org/v1\n")
    proc = _backup(
        tmp_path, "--file", "--dir", str(out_dir), DATABASE_URL=BACKUP_URL,
        BACKUP_AGE_RECIPIENT=recipient,
    )  # fmt: skip
    assert proc.returncode != 0
    assert "the new backup was written, but retention found dump names" in proc.stderr
    assert len(list(out_dir.glob("secrag-*.dump.age"))) == 2  # new dump kept, bad one kept


# --- DA-F-5: apply_roles.sh never puts a URL password on psql's command line -----------------


@needs_bash
def test_apply_roles_moves_the_url_password_to_the_environment(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "psql.log"
    psql = bin_dir / "psql"
    psql.write_text(
        '#!/usr/bin/env bash\n{ printf "argv:%s\\n" "$*"; printf "pw:%s\\n" "$PGPASSWORD"; }'
        f' >>"{log}"\ncat >/dev/null\n'
    )
    psql.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PG") and k not in {"SECRAG_PURGER_PASSWORD", "SECRAG_BACKUP_PASSWORD"}
    }
    env |= {
        "PATH": f"{bin_dir}:{env['PATH']}",
        "DATABASE_URL": "postgresql+psycopg://owner:s3cr%40t-pw@127.0.0.1:1/db",
    }
    proc = subprocess.run(
        ["bash", str(DB_SCRIPTS / "apply_roles.sh")],
        env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    lines = log.read_text().splitlines()
    argv = [line for line in lines if line.startswith("argv:")]
    assert argv and all("s3cr" not in line for line in argv)
    assert all("postgresql://owner@127.0.0.1:1/db" in line for line in argv)
    assert {line for line in lines if line.startswith("pw:")} == {"pw:s3cr@t-pw"}
    drill = (DB_SCRIPTS / "backup_drill.sh").read_text(encoding="utf-8")
    assert not re.search(r'apply_roles\.sh" "\$\(url_for', drill)


# --- backup-pull.sh (fake az) ---------------------------------------------------------------

FAKE_AZ = r"""#!/usr/bin/env bash
echo "$*" >>"$FAKE_AZ_ROOT/calls.log"
sub="$3"; shift 3
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) prefix="$2"; shift ;;
    --name) name="$2"; shift ;;
    --file) file="$2"; shift ;;
    --container-name) container="$2"; shift ;;
    --account-name | --auth-mode | --query | -o) shift ;;
  esac
  shift
done
root="$FAKE_AZ_ROOT/$container"
case "$sub" in
  list) (cd "$root" && find . -type f | sed 's|^\./||' | grep "^$prefix" | sort) ;;
  download) cp "$root/$name" "$file" ;;
esac
"""


@needs_bash
def test_backup_pull_copies_the_latest_dump_and_tombstones_and_prunes(tmp_path: Path) -> None:
    az_root = tmp_path / "az"
    container = az_root / "secrag-backups"
    (container / "backups").mkdir(parents=True)
    (container / "tombstones").mkdir()
    older, latest = _stamp(3), _stamp(1)
    for stamp in (older, latest):
        (container / "backups" / f"secrag-{stamp}.dump.age").write_bytes(
            b"age-encryption.org/v1\n" + stamp.encode()
        )
    (container / "tombstones" / f"tombstones-{latest}.jsonl").write_text("")
    bin_dir = _fake_bin(tmp_path)
    (bin_dir / "az").write_text(FAKE_AZ)
    (bin_dir / "az").chmod(0o755)
    local = tmp_path / "pulled"
    local.mkdir(mode=0o700)
    expired = local / f"secrag-{_stamp(BACKUP_RETENTION_DAYS + 1)}.dump.age"
    expired.write_bytes(b"age-encryption.org/v1\n")
    env = _env(tmp_path, FAKE_AZ_ROOT=str(az_root), BACKUP_STORAGE_ACCOUNT="secragbackups")

    def pull() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DB_SCRIPTS / "backup-pull.sh"), "--dir", str(local)],
            env=env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip

    proc = pull()
    assert proc.returncode == 0, proc.stderr
    assert (local / f"secrag-{latest}.dump.age").exists()
    assert not (local / f"secrag-{older}.dump.age").exists()  # only the latest
    assert (local / "tombstones" / f"tombstones-{latest}.jsonl").exists()
    assert not expired.exists() and f"removed {expired.name}" in proc.stdout
    calls = (az_root / "calls.log").read_text().splitlines()
    assert calls and all("--auth-mode login" in call for call in calls)
    assert "already here" in pull().stdout

    (container / "backups" / f"secrag-{_stamp(0)}.dump.age").write_bytes(b"PGDMP plaintext")
    bad = pull()
    assert bad.returncode != 0 and "not an age-encrypted file" in bad.stderr
    assert not (local / f"secrag-{_stamp(0)}.dump.age").exists()
    no_account = subprocess.run(
        ["bash", str(DB_SCRIPTS / "backup-pull.sh"), "--dir", str(local)],
        env={**env, "BACKUP_STORAGE_ACCOUNT": ""}, capture_output=True, text=True,
    )  # fmt: skip
    assert no_account.returncode != 0 and "BACKUP_STORAGE_ACCOUNT" in no_account.stderr


# --- restore.sh refusals (no database needed) -----------------------------------------------


@needs_age
def test_restore_refuses_an_unsafe_identity_or_bad_exports(
    tmp_path: Path, keys: tuple[Path, str]
) -> None:
    key, _recipient = keys
    dump = tmp_path / f"secrag-{_stamp(0)}.dump.age"
    dump.write_bytes(b"age-encryption.org/v1\n")
    exports = tmp_path / "tombstones"
    exports.mkdir()
    env = _env(tmp_path, RESTORE_DATABASE_URL="postgresql://owner:pw@127.0.0.1:1/empty")

    def restore(identity: Path, tomb: Path = exports) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DB_SCRIPTS / "restore.sh"), "--dump", str(dump), "--identity",
             str(identity), "--tombstones-dir", str(tomb)],
            env=env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip

    key.chmod(0o644)
    wide = restore(key)
    assert wide.returncode != 0 and "mode 600 or 400" in wide.stderr
    key.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(key)
    assert "no symlink" in restore(link).stderr
    (exports / f"tombstones-{_stamp(0)}.jsonl").write_text('{"user_id": "not-a-uuid"}\n')
    bad = restore(key)
    assert bad.returncode != 0 and "REFUSED" in bad.stderr
    missing = restore(key, tmp_path / "nope")
    assert missing.returncode != 0 and "does not exist" in missing.stderr
    for proc in (wide, bad, missing):
        assert "AGE-SECRET-KEY" not in proc.stdout + proc.stderr


# --- tombstone export format ----------------------------------------------------------------


def test_the_export_format_is_strict(tmp_path: Path) -> None:
    uid = uuid.uuid4()
    good = {"user_id": str(uid), "requested_at": "2026-09-30T10:00:00+00:00"}
    (tmp_path / "tombstones-20260930T100000Z.jsonl").write_text(json.dumps(good) + "\n\n")
    (tmp_path / "tombstones-20260929T100000Z.jsonl").write_text(
        json.dumps({**good, "requested_at": "2026-09-29T10:00:00+00:00"}) + "\n"
    )
    (tmp_path / "unrelated.jsonl").write_text("garbage\n")
    found, files = tombstones.read_exports(tmp_path)
    assert files == 2 and found == {uid: dt.datetime(2026, 9, 29, 10, tzinfo=dt.UTC)}
    for bad in (
        {"user_id": str(uid)},
        {**good, "email": "x@example.test"},
        {**good, "requested_at": "2026-09-30T10:00:00"},
        {**good, "user_id": "42"},
    ):
        (tmp_path / "tombstones-20260101T000000Z.jsonl").write_text(json.dumps(bad) + "\n")
        with pytest.raises(tombstones.TombstoneExportError):
            tombstones.read_exports(tmp_path)
    with pytest.raises(tombstones.TombstoneExportError, match="does not exist"):
        tombstones.read_exports(tmp_path / "missing")


def test_gitignore_keeps_backups_and_tombstone_exports_out_of_git() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert ".tombstones/" in ignored and "*.dump.age" in ignored


# --- DB: export → restore without it → union + replay --------------------------------------


@pytest.mark.db
def test_the_union_re_erases_an_account_the_restored_database_still_has(
    tmp_path: Path,
) -> None:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from rag_app.db.models import DeletionRequest, User, UserKey
    from rag_app.db.session import make_engine
    from rag_app.devtools import backup_drill

    engine = make_engine()
    try:
        with Session(engine) as session:
            ids = backup_drill.seed(session)
            keep, erase = uuid.UUID(ids["keep"]), uuid.UUID(ids["erase"])
            # the export as the purger will have written it after the erasure …
            session.add(DeletionRequest(user_id=erase, status="done"))
            session.commit()
            export = tombstones.export_tombstones(session, tmp_path / "exports")
            assert oct(export.stat().st_mode & 0o777) == "0o600"
            # … while the "restored" database predates the erasure (no tombstone, user alive)
            session.query(DeletionRequest).delete()
            session.commit()
            assert session.get(User, erase) is not None
            counts = tombstones.restore_union(session, tmp_path / "exports")
            assert counts["added"] == 1 and counts["restored"] == 0 and counts["reerased"] == 1
            assert session.get(User, erase) is None and session.get(UserKey, erase) is None
            assert session.get(User, keep) is not None
            assert session.scalar(select(DeletionRequest.status)) == "done"
            again = tombstones.restore_union(session, tmp_path / "exports")
            assert again["added"] == 0 and again["reerased"] == 0  # idempotent
            assert session.scalar(select(func.count()).select_from(User)) == 1
    finally:
        engine.dispose()
