"""Erasure scale test (T11.2b.7; gate step ``erasure-scale``) — throwaway database only.

    SCALE_ADMIN_URL=postgresql://<superuser>:<pw>@127.0.0.1:<port>/postgres \\
    SECRAG_PURGER_PASSWORD=<pw> python -m rag_app.devtools.erasure_scale run

On the gate server (loopback, never port 5432) it creates ``secrag_scale_<hex>``, applies
``db/roles.sql`` (the purger gets LOGIN) and ``alembic upgrade head``, then:

1. seeds a synthetic user with **100,000 messages** (random bytes under a key wrapped by a
   THROWAWAY master key) and ``--load-users`` ordinary accounts with real encrypted
   conversations;
2. starts the real API (``create_app()``, lifespan and start-up checks included) in a child
   process on a free loopback port — pool 5 + 10 overflow, 10 s checkout timeout; only the
   rate limiter (the load comes from one IP) and the LLM (a stub answer: chat is only a small
   sample, no GPU) are replaced; a middleware counts 5xx, lock errors and pool timeouts and
   ``/__scale/stats`` reports the pool's peak;
3. runs concurrent load (login + conversation listing + reading a conversation, a few chat
   calls) while the synthetic user sends ``DELETE /account`` (timed) — 202 and the message,
   then its token is refused, its key is gone and its email is NULL;
4. runs the purger AS ``secrag_purger`` (a real login) during the load: a throttled run is
   SIGKILLed mid-way (tombstone left ``running`` with its progress), a second run started
   meanwhile is skipped (advisory lock), a third run completes it;
5. checks: request < 200 ms; pool never exhausted (peak < pool size + overflow, no checkout
   timeout); no failed load request and no lock error; tombstone ``done``, 0 rows left; the
   longest purger transaction < 2 s. Prints the numbers; drops the database.

Counts and timings only — never an id, an email or a key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
MESSAGES = 100_000
POOL_SIZE, MAX_OVERFLOW, POOL_TIMEOUT = 5, 10, 10
REQUEST_LIMIT_MS = 200.0
PURGER_TXN_LIMIT_S = 2.0
PASSWORD = "scale-test-password"  # throwaway accounts in a throwaway database


def _fail(msg: str) -> None:
    raise SystemExit(f"erasure-scale: FAIL — {msg}")


def _check_admin_url(raw: str) -> Any:
    from sqlalchemy.engine import make_url

    url = make_url(raw)
    if (url.host or "") not in {"127.0.0.1", "localhost", "::1"}:
        _fail("SCALE_ADMIN_URL must be a loopback server (the gate project)")
    if (url.port or 5432) == 5432:
        _fail("refusing port 5432: that is the development database")
    return url.set(drivername="postgresql+psycopg")


# --- server (child process) -------------------------------------------------------------


def serve(port: int) -> None:
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from sqlalchemy import create_engine, event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from rag_app.api import deps
    from rag_app.api.app import create_app
    from rag_app.config import get_job_settings
    from rag_app.db.session import make_session_factory
    from rag_app.generation import Answer
    from rag_app.ratelimit import RateLimiter

    engine = create_engine(
        get_job_settings().database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
    )
    stats: dict[str, int] = {"peak": 0, "timeouts": 0, "lock_errors": 0, "errors_5xx": 0}
    lock = threading.Lock()

    @event.listens_for(engine.pool, "checkout")
    def _checkout(*_args: object) -> None:
        with lock:
            stats["peak"] = max(stats["peak"], engine.pool.checkedout())  # type: ignore[attr-defined]

    deps._session_factory = make_session_factory(engine)
    app = create_app()
    limiter = RateLimiter(1e9, 1e9)
    app.dependency_overrides[deps.get_rate_limiter] = lambda: limiter
    app.dependency_overrides[deps.get_answerer] = lambda: (
        lambda _s, _q, _v: Answer(text="stub answer (scale test)", abstained=True)
    )

    @app.middleware("http")
    async def _count(request: Request, call_next: Callable[..., Any]) -> Any:
        try:
            response = await call_next(request)
        except Exception as exc:
            with lock:
                stats["errors_5xx"] += 1
                if isinstance(exc, PoolTimeout):
                    stats["timeouts"] += 1
                orig = getattr(exc, "orig", None)
                if isinstance(exc, OperationalError) and getattr(orig, "sqlstate", "") in {
                    "55P03",
                    "57014",
                    "40P01",
                }:
                    stats["lock_errors"] += 1
            return JSONResponse({"detail": "internal error"}, status_code=500)
        if response.status_code >= 500:
            with lock:
                stats["errors_5xx"] += 1
        return response

    @app.get("/__scale/stats")
    def _stats() -> dict[str, int]:
        with lock:
            return {**stats, "capacity": POOL_SIZE + MAX_OVERFLOW}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# --- driver -----------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed(owner_url: str, load_users: int) -> dict[str, Any]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from rag_app.crypto import encrypt, generate_user_key, wrap_key
    from rag_app.db.models import Conversation, Message, User, UserKey
    from rag_app.security import hash_password

    engine = create_engine(owner_url)
    password_hash = hash_password(PASSWORD)
    accounts: list[str] = []
    try:
        with Session(engine) as session:
            for i in range(load_users):
                email = f"scale-load-{i}-{secrets.token_hex(3)}@example.test"
                user = User(email=email, password_hash=password_hash, email_verified=True)
                session.add(user)
                session.flush()
                data_key = generate_user_key()
                session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(data_key)))
                for c in range(3):
                    conv = Conversation(
                        user_id=user.id, title_encrypted=encrypt(data_key, f"conversation {c}")
                    )
                    session.add(conv)
                    session.flush()
                    for m in range(4):
                        body = json.dumps({"text": f"message {m}"})
                        session.add(
                            Message(
                                conversation_id=conv.id,
                                role="user" if m % 2 == 0 else "assistant",
                                content_encrypted=encrypt(data_key, body),
                            )
                        )
                accounts.append(email)
            big_email = f"scale-erase-{secrets.token_hex(3)}@example.test"
            big = User(email=big_email, password_hash=password_hash, email_verified=True)
            session.add(big)
            session.flush()
            session.add(UserKey(user_id=big.id, wrapped_key=wrap_key(generate_user_key())))
            session.commit()
            big_id = big.id
        t0 = time.monotonic()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations (id, user_id, created_at)"
                    " SELECT gen_random_uuid(), :u, now() FROM generate_series(1, 100)"
                ),
                {"u": big_id},
            )
            conn.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content_encrypted,"
                    " created_at)"
                    " SELECT gen_random_uuid(), c.id, 'user',"
                    " decode(md5(random()::text) || md5(random()::text), 'hex'), now()"
                    " FROM (SELECT id, row_number() OVER () AS n FROM conversations"
                    "        WHERE user_id = :u) c,"
                    " generate_series(1, :per) g"
                ),
                {"u": big_id, "per": MESSAGES // 100},
            )
            conn.execute(text("ANALYZE"))
            count = conn.execute(
                text(
                    "SELECT count(*) FROM messages m JOIN conversations c"
                    " ON c.id = m.conversation_id WHERE c.user_id = :u"
                ),
                {"u": big_id},
            ).scalar_one()
        if count != MESSAGES:
            _fail(f"seeded {count} messages, expected {MESSAGES}")
        return {
            "accounts": accounts,
            "big_email": big_email,
            "big_id": big_id,
            "seed_seconds": time.monotonic() - t0,
        }
    finally:
        engine.dispose()


class Load:
    """Concurrent login + conversation listing (+ a detail read, a few stub chat calls)."""

    def __init__(self, base: str, accounts: list[str], workers: int) -> None:
        self.base, self.accounts, self.workers = base, accounts, workers
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latencies: dict[str, list[float]] = {}
        self.failures: list[str] = []
        self.chat_calls = 0
        self.threads = [threading.Thread(target=self._work, args=(i,)) for i in range(workers)]

    def _record(self, kind: str, started: float, status: int, want: int = 200) -> None:
        with self.lock:
            self.latencies.setdefault(kind, []).append((time.monotonic() - started) * 1000)
            if status != want:
                self.failures.append(f"{kind} HTTP {status}")

    def _work(self, index: int) -> None:
        import httpx

        rng = random.Random(index)
        with httpx.Client(base_url=self.base, timeout=30) as client:
            while not self.stop.is_set():
                email = rng.choice(self.accounts)
                t = time.monotonic()
                r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
                self._record("login", t, r.status_code)
                if r.status_code != 200:
                    continue
                auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
                for _ in range(3):
                    t = time.monotonic()
                    r = client.get("/conversations", headers=auth)
                    self._record("list", t, r.status_code)
                if r.status_code == 200 and r.json():
                    t = time.monotonic()
                    conv = rng.choice(r.json())["id"]
                    self._record("read", t, client.get(f"/conversations/{conv}", headers=auth)
                                 .status_code)  # fmt: skip
                if index == 0 and self.chat_calls < 10:  # chat: a small sample only
                    self.chat_calls += 1
                    t = time.monotonic()
                    r = client.post("/chat", json={"question": "What is SQL injection?"},
                                    headers=auth)  # fmt: skip
                    self._record("chat", t, r.status_code)

    def __enter__(self) -> Load:
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=60)


def _purger_env(purger_url: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOMBSTONE_STORAGE")}
    env["DATABASE_URL"] = purger_url
    env["PYTHONPATH"] = str(BACKEND / "src")
    return env


def _tombstone(owner_engine: Any, user_id: Any) -> tuple[str, dict[str, int]]:
    from sqlalchemy import text

    with owner_engine.connect() as conn:
        status, progress = conn.execute(
            text("SELECT status, progress FROM deletion_requests WHERE user_id = :u"),
            {"u": user_id},
        ).one()
    return str(status), dict(progress)


def run(args: argparse.Namespace) -> int:
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, text

    admin = _check_admin_url(os.environ.get("SCALE_ADMIN_URL", ""))
    purger_pw = os.environ.get("SECRAG_PURGER_PASSWORD", "")
    if not purger_pw:
        _fail("SECRAG_PURGER_PASSWORD is not set (the purger logs in as secrag_purger)")
    db = f"secrag_scale_{secrets.token_hex(4)}"
    owner_url = admin.set(database=db).render_as_string(hide_password=False)
    purger_url = admin.set(
        database=db, username="secrag_purger", password=purger_pw
    ).render_as_string(hide_password=False)
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    server: subprocess.Popen[bytes] | None = None
    owner_engine = create_engine(owner_url)
    workdir = Path(tempfile.mkdtemp(prefix="secrag-scale-"))
    # No .env of the checkout (backend/.env points at the development database and holds the
    # real key): every setting this test needs comes from the environment it builds.
    os.chdir(workdir)
    t_start = time.monotonic()
    try:
        # roles (purger LOGIN) + migrations
        roles_env = {**os.environ, "PGPASSWORD": admin.password or ""}
        plain = admin.set(database=db, drivername="postgresql", password=None)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/db/apply_roles.sh"),
             plain.render_as_string(hide_password=False)],
            env=roles_env, check=True, capture_output=True, timeout=120,
        )  # fmt: skip
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env={**os.environ, "DATABASE_URL": owner_url, "PYTHONPATH": str(BACKEND / "src")},
            check=True,
            capture_output=True,
            timeout=300,
        )
        master = Fernet.generate_key().decode()
        os.environ["DATA_MASTER_KEY"] = master  # this process wraps the seed keys with it
        seed = _seed(owner_url, args.load_users)
        print(
            f"  seeded {MESSAGES} messages for the synthetic user in {seed['seed_seconds']:.1f} s"
            f" + {args.load_users} load accounts (3 conversations x 4 messages)"
        )

        port = _free_port()
        server_env = {
            **os.environ,
            "DATABASE_URL": owner_url,
            "DATA_MASTER_KEY": master,
            "JWT_SECRET": secrets.token_urlsafe(48),
            "ENV": "dev",
            "PYTHONPATH": str(BACKEND / "src"),
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "rag_app.devtools.erasure_scale", "serve", "--port", str(port)],
            cwd=workdir,
            env=server_env,
            stdout=subprocess.DEVNULL,
            stderr=open(workdir / "server.log", "wb"),  # noqa: SIM115
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(240):
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None:
                _fail("the API did not start: " + (workdir / "server.log").read_text()[-400:])
            time.sleep(0.5)
        else:
            _fail("the API did not answer /health within 120 s")

        with httpx.Client(base_url=base, timeout=30) as client:
            r = client.post("/auth/login", json={"email": seed["big_email"], "password": PASSWORD})
            if r.status_code != 200:
                _fail(f"login of the synthetic user: HTTP {r.status_code}")
            big_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

            with Load(base, seed["accounts"], args.workers) as load:
                time.sleep(args.warmup)
                t = time.monotonic()
                r = client.delete("/account", headers=big_auth)
                delete_ms = (time.monotonic() - t) * 1000
                if r.status_code != 202:
                    _fail(f"DELETE /account: HTTP {r.status_code}")
                message = r.json()["detail"]
                me = client.get("/auth/me", headers=big_auth).status_code
                with owner_engine.connect() as conn:
                    key_left, email_left = conn.execute(
                        text(
                            "SELECT (SELECT count(*) FROM user_keys WHERE user_id = :u),"
                            " (SELECT count(*) FROM users WHERE id = :u AND email IS NOT NULL)"
                        ),
                        {"u": seed["big_id"]},
                    ).one()
                status, _ = _tombstone(owner_engine, seed["big_id"])

                # purger as secrag_purger: throttled run killed mid-way; overlap skipped
                penv = _purger_env(purger_url)
                cmd = [sys.executable, "-m", "rag_app.erasure", "purge",
                       "--export-dir", str(workdir / "tombstones")]  # fmt: skip
                first = subprocess.Popen(
                    [*cmd, "--pause-seconds", "0.05"],
                    env=penv, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )  # fmt: skip
                progress: dict[str, int] = {}
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    _, progress = _tombstone(owner_engine, seed["big_id"])
                    if progress.get("messages", 0) >= 10_000:
                        break
                    time.sleep(0.02)
                overlap = subprocess.run(
                    cmd, env=penv, cwd=workdir, capture_output=True, text=True, timeout=120
                )
                first.send_signal(signal.SIGKILL)
                first.wait(timeout=30)
                killed_status, killed_progress = _tombstone(owner_engine, seed["big_id"])
                t = time.monotonic()
                for _ in range(20):  # the killed run's lock goes with its connection
                    final = subprocess.run(
                        cmd, env=penv, cwd=workdir, capture_output=True, text=True, timeout=600
                    )
                    if "skipped" not in final.stdout:
                        break
                    time.sleep(0.5)
                purge_seconds = time.monotonic() - t
                time.sleep(args.cooldown)
            stats = client.get("/__scale/stats").json()

        done_status, done_progress = _tombstone(owner_engine, seed["big_id"])
        with owner_engine.connect() as conn:
            rows_left = conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM users WHERE id = :u)"
                    " + (SELECT count(*) FROM conversations WHERE user_id = :u)"
                    " + (SELECT count(*) FROM messages m JOIN conversations c"
                    "    ON c.id = m.conversation_id WHERE c.user_id = :u)"
                ),
                {"u": seed["big_id"]},
            ).scalar_one()
            runs = conn.execute(
                text("SELECT count(*) FROM purger_runs WHERE finished_at IS NOT NULL")
            ).scalar_one()
        longest = re.search(r"longest ([0-9.]+) s", final.stdout)
        longest_s = float(longest.group(1)) if longest else float("inf")

        lat = {k: v for k, v in load.latencies.items() if v}
        total_requests = sum(len(v) for v in lat.values())
        print(f"  DELETE /account: HTTP 202 in {delete_ms:.1f} ms (limit {REQUEST_LIMIT_MS:.0f})")
        print(f"  message: {message}")
        print(
            f"  right after: token -> HTTP {me}; user keys left {key_left}; emails left"
            f" {email_left}; tombstone {status}"
        )
        for kind, values in sorted(lat.items()):
            p95 = statistics.quantiles(values, n=20)[-1] if len(values) >= 20 else max(values)
            print(
                f"  load {kind:<5} n={len(values):<5} median {statistics.median(values):6.1f} ms"
                f"  p95 {p95:6.1f} ms  max {max(values):7.1f} ms"
            )
        print(
            f"  load: {total_requests} requests by {args.workers} workers,"
            f" {len(load.failures)} failed; server 5xx {stats['errors_5xx']}, lock errors"
            f" {stats['lock_errors']}, pool timeouts {stats['timeouts']}; pool peak"
            f" {stats['peak']} of {stats['capacity']} connections"
        )
        print(
            f"  purger: killed at messages {killed_progress.get('messages', 0)} (tombstone"
            f" {killed_status}); overlapping run: "
            f"{'skipped' if 'skipped' in overlap.stdout else 'NOT skipped'}; next run"
            f" {purge_seconds:.1f} s -> tombstone {done_status}, rows left {rows_left},"
            f" longest transaction {longest_s:.3f} s; purger_runs finished {runs}"
        )
        print("  " + final.stdout.strip().replace("\n", "\n  "))

        checks = {
            f"request < {REQUEST_LIMIT_MS:.0f} ms": delete_ms < REQUEST_LIMIT_MS,
            "202 message states the 14-day constant": message.endswith(
                f"within {_retention()} days."
            ),
            "token refused right after": me == 401,
            "key gone and email scrubbed right after": key_left == 0 and email_left == 0,
            "tombstone pending right after": status == "pending",
            "pool never exhausted": stats["peak"] < stats["capacity"] and stats["timeouts"] == 0,
            "no failed load request": not load.failures and stats["errors_5xx"] == 0,
            "no lock error reached other requests": stats["lock_errors"] == 0,
            "killed mid-way (tombstone running)": killed_status == "running"
            and 0 < killed_progress.get("messages", 0) < MESSAGES,
            "overlapping run skipped": "skipped" in overlap.stdout,
            "next run completes: tombstone done, 0 rows": final.returncode == 0
            and done_status == "done"
            and rows_left == 0
            and done_progress.get("messages", 0) == MESSAGES,
            f"purger transactions < {PURGER_TXN_LIMIT_S:.0f} s": longest_s < PURGER_TXN_LIMIT_S,
        }
        failed = [name for name, ok in checks.items() if not ok]
        for name, ok in checks.items():
            print(f"  {'ok' if ok else 'FAIL'}: {name}")
        if load.failures:
            print(f"  first failures: {sorted(set(load.failures))[:5]}")
        print(
            f"erasure-scale: {'PASS' if not failed else 'FAIL'}"
            f" ({time.monotonic() - t_start:.0f} s)"
        )
        return 0 if not failed else 1
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
        owner_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        admin_engine.dispose()
        for path in sorted(workdir.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        workdir.rmdir()


def _retention() -> int:
    from rag_app.retention import BACKUP_RETENTION_DAYS

    return BACKUP_RETENTION_DAYS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.devtools.erasure_scale")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--load-users", type=int, default=20)
    r.add_argument("--warmup", type=float, default=3.0)
    r.add_argument("--cooldown", type=float, default=2.0)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(args.port)
        return 0
    started = dt.datetime.now(dt.UTC)
    print(f"erasure-scale: start {started:%H:%M:%S}Z (throwaway database on the gate server)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
