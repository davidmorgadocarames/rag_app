"""11.1 diagnosis helpers (T11.1.3 preparation, T11.1.5): the key snippet that runs inside
the backend through `az containerapp exec`, its one-liner wrapper, and restart_check's
safety refusals. No Azure, no compose stack."""

from __future__ import annotations

import base64
import importlib.util
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
SNIPPET = REPO_ROOT / "scripts" / "azure" / "diag_keys.py"
ONELINER = REPO_ROOT / "scripts" / "azure" / "exec_oneliner.py"
RESTART_CHECK = REPO_ROOT / "scripts" / "restart_check.sh"
SRC = REPO_ROOT / "backend" / "src"


def _oneliner_module():
    spec = importlib.util.spec_from_file_location("exec_oneliner", ONELINER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE = 'import sys\nprint("ok", len(sys.argv))\nprint("line \\"two\\" $HOME `x`")\n'


def test_split_oneliner_is_one_argument_and_runs_unchanged() -> None:
    cmd = _oneliner_module().oneliner(SOURCE, "split")
    argv = cmd.split()  # a whitespace splitter, no shell
    assert argv[:2] == ["python", "-c"] and len(argv) == 3
    out = subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True, check=True)
    assert out.stdout == 'ok 1\nline "two" $HOME `x`\n'


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_shell_oneliner_survives_a_shell() -> None:
    cmd = _oneliner_module().oneliner(SOURCE, "shell")
    shell_cmd = cmd.replace("python", f'"{sys.executable}"', 1)
    out = subprocess.run(["sh", "-c", shell_cmd], capture_output=True, text=True, check=True)
    assert out.stdout.splitlines()[0] == "ok 1"


def _run_snippet(env: dict[str, str], cwd: Path, *, oneliner: bool = False) -> list[str]:
    full_env = {**os.environ, **env, "PYTHONPATH": str(SRC)}
    if oneliner:
        code = _oneliner_module().oneliner(SNIPPET.read_text(encoding="utf-8")).split()[2]
        args = [sys.executable, "-c", code]
    else:
        args = [sys.executable, str(SNIPPET)]
    proc = subprocess.run(args, env=full_env, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    return proc.stdout.splitlines()


def test_snippet_prints_only_the_verdicts_when_the_db_is_unreachable(tmp_path: Path) -> None:
    lines = _run_snippet(
        {
            "DATABASE_URL": "postgresql+psycopg://nobody:secretpw@127.0.0.1:1/none",
            "DATA_MASTER_KEY": "not-a-fernet-key",
            "JWT_SECRET": "short",
        },
        tmp_path,  # no .env here: settings come from the environment only
    )
    assert lines == [
        "N: error (OperationalError)",
        "OK: error (OperationalError)",
        "KO: error (OperationalError)",
        "JWT_SECRET length >= 32: KO",
        "DATA_MASTER_KEY valid Fernet: KO",
    ]


def test_snippet_says_unknown_when_the_settings_cannot_be_loaded(tmp_path: Path) -> None:
    """DA-D-9: nothing evaluated → "unknown", never a KO that looks like a checked failure."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-I", str(SNIPPET)],  # isolated: rag_app is not importable
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "N: error (ModuleNotFoundError)",
        "OK: -",
        "KO: -",
        "JWT_SECRET length >= 32: unknown",
        "DATA_MASTER_KEY valid Fernet: unknown",
    ]


@pytest.mark.db
@pytest.mark.parametrize("oneliner", [False, True], ids=["file", "oneliner"])
def test_snippet_counts_unwrappable_keys(db_engine, tmp_path: Path, oneliner: bool) -> None:
    from sqlalchemy.orm import Session

    from rag_app.db.models import User, UserKey

    master = Fernet.generate_key()
    other = Fernet.generate_key()
    emails = [f"diag-{uuid.uuid4().hex}@example.com" for _ in range(3)]
    with Session(db_engine) as session:
        users = [User(email=e, password_hash="x") for e in emails]
        session.add_all(users)
        session.flush()
        for user, key in zip(users, (master, master, other), strict=True):
            wrapped = Fernet(key).encrypt(Fernet.generate_key())
            session.add(UserKey(user_id=user.id, wrapped_key=wrapped))
        session.commit()
        ids = [u.id for u in users]
    try:
        env = {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "DATA_MASTER_KEY": master.decode(),
            "JWT_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        }
        lines = _run_snippet(env, tmp_path, oneliner=oneliner)
        n = int(lines[0].split(": ")[1])
        assert n >= 3  # the session DB may hold other tests' users; ours: 2 OK, 1 KO
        assert int(lines[1].split(": ")[1]) >= 2 and int(lines[2].split(": ")[1]) >= 1
        assert lines[3:] == ["JWT_SECRET length >= 32: OK", "DATA_MASTER_KEY valid Fernet: OK"]
        out = "\n".join(lines)
        assert not any(e in out for e in emails) and master.decode() not in out
    finally:
        with Session(db_engine) as session:
            for model in (UserKey, User):
                column = UserKey.user_id if model is UserKey else User.id
                session.query(model).filter(column.in_(ids)).delete()
            session.commit()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "args",
    [
        ["--project", "rag_ia"],
        ["--project", "rag_app"],
        ["--project", "secrag-x", "--restart-project", "rag_app"],
        ["--project", "Bad!"],
    ],
)
def test_restart_check_refuses_the_development_projects(args: list[str]) -> None:
    proc = subprocess.run(
        ["bash", str(RESTART_CHECK), *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "REFUSED" in proc.stderr
    assert "project" in proc.stderr  # refused for the project, before touching docker


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("port", ["8000", "5432", "3000", "11434", "x"])
def test_restart_check_refuses_development_ports(port: str) -> None:
    proc = subprocess.run(
        ["bash", str(RESTART_CHECK), "--project", "secrag-rc-unit"],
        env={**os.environ, "RESTART_CHECK_API_PORT": port},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2 and "REFUSED" in proc.stderr and "port" in proc.stderr.lower()


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs a reachable docker")
def test_restart_check_refuses_a_busy_api_port(tmp_path: Path) -> None:
    """DA-D-7: something already answering on the API port (e.g. a native uvicorn on WSL's
    127.0.0.1) must stop the check before any container starts."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            import time

            time.sleep(0.1)
        proc = subprocess.run(
            ["bash", str(RESTART_CHECK), "--project", "secrag-rc-unit"],
            env={**os.environ, "RESTART_CHECK_API_PORT": str(port)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "already listens/answers" in proc.stderr
    assert "throwaway volume" not in proc.stdout  # refused before creating anything
