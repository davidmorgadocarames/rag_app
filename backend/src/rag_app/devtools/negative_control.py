"""Negative control for the erasure-at-scale latency criterion (DEBT-017 part 2, DA-G1-7).

A ONE-OFF proof that the ``erasure-scale`` gate step's "request < 200 ms" criterion
(``rag_app.devtools.erasure_scale.REQUEST_LIMIT_MS``) actually has teeth: it must be able to
show RED, not just always be green by construction. ``rag_app.erasure.erase_user`` is an
existing SYNCHRONOUS, cascading hard delete (today only ever called from the offline
``key_recovery.sh erase`` tool, never from the API) -- this script patches a THROWAWAY git
worktree so ``DELETE /account`` calls it DIRECTLY, in the request's own transaction, instead
of the real (asynchronous, tombstone + batched purger) ``request_erasure`` path, then runs
the exact same single-big-user erasure measurement ``erasure-scale`` uses and shows the
median request latency FAILS the same < 200 ms criterion.

Never run by any gate step on purpose -- it is SUPPOSED to fail. Run by hand, once, to
collect evidence for DEBT-017's closure, then thrown away:

    SCALE_ADMIN_URL=postgresql://<superuser>:<pw>@127.0.0.1:<port>/postgres \\
    NEGCTL_MESSAGES=1000000 python -m rag_app.devtools.negative_control run

Same environment rules as ``rag_app.devtools.erasure_scale`` (loopback, never port 5432).
The worktree is created under a temp directory and removed again at the end (success or
failure) -- `git worktree list` must show nothing left over once this exits.

``NEGCTL_MESSAGES`` (default: ``erasure_scale.MESSAGES``, 100,000 -- the same size
``erasure-scale`` uses): a single unbatched cascading ``DELETE`` (FK ``ON DELETE CASCADE``)
of 100,000 messages turned out to still be fast enough on a real run (72 ms, under the
200 ms limit) -- Postgres' own cascade is a set-based operation, not the batched,
checkpointed work the real purger does. 1,000,000 messages reliably exceeded it (717 ms,
real run, DEBT-017 evidence) -- large enough to make the point without taking long to seed.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import rag_app.devtools.erasure_scale as erasure_scale_module
from rag_app.devtools.erasure_scale import (
    MESSAGES,
    PASSWORD,
    REQUEST_LIMIT_MS,
    _check_admin_url,
    _free_port,
    _seed,
    _settle,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


def _fail(msg: str) -> None:
    raise SystemExit(f"negative-control: FAIL — {msg}")


def _patch_worktree_to_call_erase_user_synchronously(worktree: Path) -> None:
    """The ONLY change made inside the throwaway worktree: ``DELETE /account`` calls
    ``rag_app.erasure.erase_user`` (synchronous, cascading) instead of ``request_erasure``
    (asynchronous: tombstone + batched purger). Never committed anywhere -- the worktree is
    removed as soon as the measurement is done."""
    auth_py = worktree / "backend" / "src" / "rag_app" / "api" / "auth.py"
    original = auth_py.read_text(encoding="utf-8")
    patched = original.replace(
        "    request_erasure,\n", "    erase_user,\n    request_erasure,\n", 1
    )
    patched = patched.replace(
        "        request_erasure(session, user.id)\n",
        "        erase_user(session, user.id)  # NEGATIVE CONTROL (DA-G1-7): synchronous\n",
        1,
    )
    if patched == original:
        _fail(f"the expected lines were not found in {auth_py} -- auth.py must have changed")
    auth_py.write_text(patched, encoding="utf-8")


def serve(port: int) -> None:  # pragma: no cover - exercised as a child process, not by pytest
    import uvicorn

    from rag_app.api.app import create_app
    from rag_app.generation import Answer
    from rag_app.ratelimit import RateLimiter

    app = create_app()
    from rag_app.api import deps

    limiter = RateLimiter(1e9, 1e9)
    app.dependency_overrides[deps.get_rate_limiter] = lambda: limiter
    app.dependency_overrides[deps.get_answerer] = lambda: (
        lambda _s, _q, _v: Answer(text="stub answer (negative control)", abstained=True)
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def run() -> int:
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, text

    admin = _check_admin_url(os.environ.get("SCALE_ADMIN_URL", ""))
    db = f"secrag_negctl_{secrets.token_hex(4)}"
    owner_url = admin.set(database=db).render_as_string(hide_password=False)
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    worktree: Path | None = None
    server: subprocess.Popen[bytes] | None = None
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    owner_engine = create_engine(owner_url)
    workdir = Path(tempfile.mkdtemp(prefix="secrag-negctl-"))
    try:
        worktree = Path(tempfile.mkdtemp(prefix="secrag-negctl-worktree-")) / "wt"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        _patch_worktree_to_call_erase_user_synchronously(worktree)

        roles_env = {**os.environ, "PGPASSWORD": admin.password or ""}
        plain = admin.set(database=db, drivername="postgresql", password=None)
        subprocess.run(
            ["bash", str(REPO_ROOT / "scripts/db/apply_roles.sh"),
             plain.render_as_string(hide_password=False)],
            env=roles_env, check=True, capture_output=True, timeout=120,
        )  # fmt: skip
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT / "backend",
            env={
                **os.environ,
                "DATABASE_URL": owner_url,
                "PYTHONPATH": str(REPO_ROOT / "backend" / "src"),
            },  # fmt: skip
            check=True,
            capture_output=True,
            timeout=300,
        )
        master = Fernet.generate_key().decode()
        os.environ["DATA_MASTER_KEY"] = master
        # A single unbatched cascading DELETE (FK ON DELETE CASCADE) of the SAME 100,000
        # messages erasure-scale uses turned out to be fast enough on this machine to still
        # pass the 200 ms criterion (first run, evidence below) -- scaled up via
        # NEGCTL_MESSAGES (module-global override, same pattern `_seed` always reads) until
        # the synchronous path's cost genuinely shows through.
        messages = int(os.environ.get("NEGCTL_MESSAGES", MESSAGES))
        erasure_scale_module.MESSAGES = messages
        seed = _seed(owner_url, load_users=0, big_users=1)
        _settle(owner_url, admin_engine)
        print(f"  seeded 1 synthetic user x {messages} messages in {seed['seed_seconds']:.1f} s")

        port = _free_port()
        server_env = {
            **os.environ,
            "DATABASE_URL": owner_url,
            "DATA_MASTER_KEY": master,
            "JWT_SECRET": secrets.token_urlsafe(48),
            "CSRF_SECRET": secrets.token_urlsafe(48),
            "LOCKOUT_HMAC_SECRET": secrets.token_urlsafe(48),
            "ENV": "dev",
            "PYTHONPATH": str(worktree / "backend" / "src"),  # the PATCHED source
        }
        serve_cmd = [
            sys.executable,
            "-m",
            "rag_app.devtools.negative_control",
            "serve",
            "--port",
            str(port),
        ]
        server = subprocess.Popen(  # noqa: SIM115
            serve_cmd,
            cwd=workdir,
            env=server_env,
            stdout=subprocess.DEVNULL,
            stderr=open(workdir / "server.log", "wb"),
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(240):
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None:
                _fail(
                    "the patched API did not start: " + (workdir / "server.log").read_text()[-400:]
                )
            time.sleep(0.5)
        else:
            _fail("the patched API did not answer /health within 120 s")

        from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
        from rag_app.sessions import SESSION_COOKIE_NAME

        email, _uid = seed["big"][0]
        samples: list[float] = []
        with httpx.Client(base_url=base, timeout=60) as client:
            login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
            if login.status_code != 200:
                _fail(f"login of the synthetic user: HTTP {login.status_code}")
            cookies = {SESSION_COOKIE_NAME: login.cookies.get(SESSION_COOKIE_NAME) or ""}
            csrf = {CSRF_HEADER_NAME: login.cookies.get(CSRF_COOKIE_NAME) or ""}
            t = time.monotonic()
            r = client.delete("/account", headers=csrf, cookies=cookies)
            ms = (time.monotonic() - t) * 1000
            if r.status_code != 202:
                _fail(f"DELETE /account: HTTP {r.status_code}")
            samples.append(ms)
            print(f"  DELETE /account (synchronous erase_user): HTTP 202 in {ms:.1f} ms round trip")

        median_ms = statistics.median(samples)
        passes_the_real_criterion = median_ms < REQUEST_LIMIT_MS
        verdict = "PASS (unexpected!)" if passes_the_real_criterion else "FAIL"
        print(
            f"  median {median_ms:.1f} ms vs the erasure-scale limit {REQUEST_LIMIT_MS:.0f} ms"
            f" -- the real criterion is {verdict}"
        )
        if passes_the_real_criterion:
            print(
                "negative-control: FAIL — the synchronous path did NOT exceed the latency"
                " criterion; this proves nothing (seed more messages or investigate)",
                file=sys.stderr,
            )
            return 1
        print(
            "negative-control: PASS — the latency criterion correctly shows RED for a"
            " synchronous erasure path (DEBT-017/DA-G1-7 evidence)"
        )
        return 0
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
        if worktree is not None and worktree.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                timeout=60,
            )
            shutil.rmtree(worktree.parent, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m rag_app.devtools.negative_control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(args.port)
        return 0
    started = dt.datetime.now(dt.UTC)
    print(f"negative-control: start {started:%H:%M:%S}Z (throwaway database + git worktree)")
    return run()


if __name__ == "__main__":
    sys.exit(main())
