"""Log hygiene (T11.2.15, R6-5): no log record carries an e-mail address or a token.

- The emailer never logs the address or the verification link (it holds a live token).
- uvicorn's access log never shows query strings (``/auth/verify?token=…``): the
  ``AccessLogRedactor`` filter, installed by ``create_app``.
- Database errors never render bound parameters (``hide_parameters``), so a traceback of a
  failed INSERT into ``users`` does not print the e-mail.
- The whole auth flow, served by a **real uvicorn server** (its real access and error
  loggers, plus SQLAlchemy's statement log switched on), leaves no e-mail, password,
  verification token or access token in any record.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rag_app.logsafe import REDACTED, AccessLogRedactor, install_log_redaction, redact_query

# --- helpers ------------------------------------------------------------------------------


class _Collect(logging.Handler):
    """Keeps every record it sees, fully rendered (message + traceback)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.rendered: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.rendered.append(self.format(record))


def _uvicorn_access_record(path: str) -> logging.LogRecord:
    """A record shaped exactly like uvicorn's access log line (httptools/h11 protocols)."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", path, "1.1", 200),
        None,
    )


def _remove_redactors() -> None:
    access = logging.getLogger("uvicorn.access")
    for f in [f for f in access.filters if isinstance(f, AccessLogRedactor)]:
        access.removeFilter(f)


def _leaks(rendered: list[str], secrets: list[str]) -> list[str]:
    return [line for line in rendered for secret in secrets if secret and secret in line]


# --- unit: emailer, redactor, engine ------------------------------------------------------


def test_emailer_without_smtp_logs_neither_address_nor_link(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from rag_app.emailer import send_verification_email

    monkeypatch.setenv("SMTP_HOST", "")
    token = "raw-verification-token-" + uuid.uuid4().hex
    with caplog.at_level(logging.DEBUG):
        send_verification_email("alice.leak@example.test", token)
    assert caplog.records, "the missing SMTP configuration is still reported"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "alice.leak" not in text and "example.test" not in text
    assert token not in text and "/auth/verify" not in text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/health", "/health"),
        ("/auth/verify?token=abc", f"/auth/verify?token={REDACTED}"),
        ("/x?a=1&b=&c", f"/x?a={REDACTED}&b={REDACTED}&{REDACTED}"),
        ("/x?", f"/x?{REDACTED}"),
    ],
)
def test_redact_query(path: str, expected: str) -> None:
    assert redact_query(path) == expected


def test_the_access_log_filter_redacts_uvicorns_record_shape() -> None:
    record = _uvicorn_access_record("/auth/verify?token=SECRET-TOKEN")
    assert AccessLogRedactor().filter(record) is True  # never drops the line
    message = record.getMessage()
    assert "SECRET-TOKEN" not in message
    assert f'"GET /auth/verify?token={REDACTED} HTTP/1.1" 200' in message


def test_the_access_log_filter_redacts_a_preformatted_line() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, "GET /auth/verify?token=SECRET 200", None, None
    )
    AccessLogRedactor().filter(record)
    assert "SECRET" not in record.getMessage()


def test_create_app_installs_the_redactor_once() -> None:
    from rag_app.api.app import create_app

    _remove_redactors()
    create_app()
    create_app()
    install_log_redaction()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, AccessLogRedactor) for f in access.filters) == 1


def test_database_errors_never_render_bound_parameters() -> None:
    from sqlalchemy import text

    from rag_app.db.session import make_engine

    engine = make_engine("sqlite://")
    email = "bob.leak@example.test"
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (email TEXT UNIQUE)"))
        conn.execute(text("INSERT INTO users VALUES (:e)"), {"e": email})
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        conn.execute(text("INSERT INTO users VALUES (:e)"), {"e": email})
    assert email not in str(caught.value)
    assert "hidden" in str(caught.value)


# --- a real uvicorn server ----------------------------------------------------------------


class _Server:
    def __init__(self, app_factory: Callable[[], Any]) -> None:
        import uvicorn

        # Same order as production: uvicorn configures logging, then imports/creates the app.
        self.config = uvicorn.Config(
            app_factory, factory=True, host="127.0.0.1", port=0, lifespan="off", access_log=True
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        # urllib, not httpx: the client itself must not add log records of its own.
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:  # noqa: S310 - local test server
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")


@pytest.fixture()
def captured_logs() -> Iterator[_Collect]:
    """Every logger that can print in the API process, at DEBUG, incl. SQL statements."""
    handler = _Collect()
    _remove_redactors()  # only what the app under test installs may redact
    names = ["", "uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine"]
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).handlers[:]) for n in names}
    try:
        yield handler
    finally:
        for name, (level, handlers) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            if handler in logger.handlers:
                logger.removeHandler(handler)
            for h in handlers:
                if h not in logger.handlers:
                    logger.addHandler(h)


def _attach(handler: _Collect) -> None:
    """After uvicorn's dictConfig (it replaces handlers on its own loggers)."""
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
    logging.getLogger("").setLevel(logging.DEBUG)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)  # statement + params log


def test_uvicorn_access_log_never_shows_a_query_string(captured_logs: _Collect) -> None:
    from rag_app.api.app import create_app

    secret = "SECRET-" + uuid.uuid4().hex
    with _Server(create_app) as server:
        _attach(captured_logs)
        status, _ = server.call("GET", f"/health?token={secret}")
        assert status == 200
        time.sleep(0.2)
    access = [r for r in captured_logs.records if r.name == "uvicorn.access"]
    assert access, "the access log line is still written"
    assert f"/health?token={REDACTED}" in "\n".join(captured_logs.rendered)
    assert not _leaks(captured_logs.rendered, [secret])


@pytest.mark.db
def test_the_whole_auth_flow_logs_no_email_or_token(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, captured_logs: _Collect
) -> None:
    from rag_app.api import auth
    from rag_app.api.app import create_app
    from rag_app.db.models import User

    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")  # the dev path that used to log the link
    monkeypatch.setenv("REQUIRE_EMAIL_VERIFICATION", "true")

    issued: list[str] = []
    real_generate = auth.generate_verification_token

    def _recording() -> str:
        issued.append(real_generate())
        return issued[-1]

    monkeypatch.setattr(auth, "generate_verification_token", _recording)

    email = f"carol.{uuid.uuid4().hex[:8]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    access_tokens: list[str] = []
    with _Server(create_app) as server:
        _attach(captured_logs)
        status, body = server.call("POST", "/auth/register", {"email": email, "password": password})
        assert status == 201
        access_tokens.append(body["access_token"])
        jwt = body["access_token"]
        credentials = {"email": email, "password": password}
        assert server.call("POST", "/auth/register", credentials)[0] == 409
        assert server.call("POST", "/auth/login", {"email": email, "password": "wrong"})[0] == 401
        assert server.call("POST", "/auth/login", credentials)[0] == 403
        status, body = server.call("POST", "/auth/resend-verification", token=jwt)
        assert status == 200 and body["verification_link"]  # returned to the UI, not logged
        assert server.call("GET", "/auth/verify?token=not-a-real-token")[0] == 400
        assert server.call("GET", f"/auth/verify?token={issued[-1]}")[0] == 200
        status, body = server.call("POST", "/auth/login", {"email": email, "password": password})
        assert status == 200
        access_tokens.append(body["access_token"])
        assert server.call("GET", "/auth/me", token=body["access_token"])[0] == 200
        assert server.call("GET", "/conversations", token=body["access_token"])[0] == 200
        assert server.call("DELETE", "/account", token=body["access_token"])[0] == 202
        assert server.call("GET", "/auth/me", token=body["access_token"])[0] == 401
        assert server.call("POST", "/auth/login", {"email": email, "password": password})[0] == 401
        time.sleep(0.2)

    assert len(issued) == 2
    names = {r.name for r in captured_logs.records}
    assert "uvicorn.access" in names and "sqlalchemy.engine.Engine" in names  # really captured
    assert f"/auth/verify?token={REDACTED}" in "\n".join(captured_logs.rendered)
    local_part = email.split("@", 1)[0]
    secrets = [email, local_part, password, *issued, *access_tokens]
    assert not _leaks(captured_logs.rendered, secrets)

    # the erasure scrubbed the address, so nothing is left to leak later either
    with Session(db_engine) as session:
        assert session.query(User).filter(User.email == email).count() == 0


# --- DA-G2-1: a concurrent sign-up never leaks the address ----------------------------------


@pytest.mark.db
def test_a_racing_duplicate_sign_up_is_a_409_and_logs_no_email(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, captured_logs: _Collect
) -> None:
    """Two registrations of one address at once (double click): the API's existence check
    passes for both, the unique index rejects the second INSERT. On Postgres the driver's
    message carries ``DETAIL: Key (email)=(…) already exists`` — it must end as a 409, never
    as a 500 whose traceback prints the address (SQLite's message has no value, so this runs
    on the real database)."""
    from sqlalchemy import create_engine, text

    from rag_app.api.app import create_app

    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")

    email = f"dave.{uuid.uuid4().hex[:8]}@example.test"
    password = "correct horse battery staple " + uuid.uuid4().hex[:6]
    result: dict[str, Any] = {}
    with _Server(create_app) as server:
        _attach(captured_logs)
        # The test's own statements are logged too (sqlalchemy.engine at INFO): its engine
        # hides the bound values like the app's does, so only the app is under test.
        racer_engine = create_engine(db_engine.url, hide_parameters=True)
        with racer_engine.connect() as racer:
            # The other registration: its row is inserted but not committed yet, so the API's
            # SELECT does not see it and its INSERT waits on the unique index.
            racer.begin()
            racer.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, email_verified)"
                    " VALUES (:i, :e, 'x', false)"
                ),
                {"i": uuid.uuid4(), "e": email},
            )
            call = threading.Thread(
                target=lambda: result.update(
                    r=server.call("POST", "/auth/register", {"email": email, "password": password})
                )
            )
            call.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:  # wait until the API's INSERT is blocked
                waiting = racer.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                    )
                ).scalar()
                if waiting:
                    break
                time.sleep(0.05)
            racer.commit()  # the other registration wins → unique violation in the API
            call.join(timeout=30)
        racer_engine.dispose()
        time.sleep(0.2)
    status, body = result["r"]
    assert status == 409 and body == {"detail": "email already registered"}
    assert email not in json.dumps(body)
    assert not _leaks(captured_logs.rendered, [email, email.split("@", 1)[0]])
    with db_engine.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}).scalar()
            == 1
        )
        conn.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})


def test_postgres_logs_no_detail_lines_locally() -> None:
    """DA-G2-1: the local and gate Postgres run with log_error_verbosity=terse (no DETAIL
    line with a key value in the server log); Azure gets the same in row 40 (runbook)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for compose in ("docker-compose.yml", "compose.gate.yml"):
        assert "log_error_verbosity=terse" in (root / compose).read_text(), compose


# --- DA-G2-5: the verification link only ever goes to the UI in dev -------------------------


@pytest.mark.db
@pytest.mark.parametrize("env", ["dev", "prod"])
def test_resend_returns_the_link_only_in_dev(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app
    from rag_app.crypto import generate_user_key, wrap_key
    from rag_app.db.models import User, UserKey
    from rag_app.security import create_token

    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("SMTP_HOST", "")
    with Session(db_engine) as session:
        user = User(email=f"erin.{uuid.uuid4().hex[:8]}@example.test", password_hash="x")
        session.add(user)
        session.flush()
        session.add(UserKey(user_id=user.id, wrapped_key=wrap_key(generate_user_key())))
        session.commit()
        user_id = user.id
    client = TestClient(create_app())
    res = client.post(
        "/auth/resend-verification",
        headers={"Authorization": f"Bearer {create_token(str(user_id))}"},
    )
    assert res.status_code == 200
    body = res.json()
    if env == "dev":
        assert body["verification_link"] and "token=" in body["verification_link"]
    else:
        assert body["verification_link"] is None
        assert "token" not in json.dumps(body) and body["detail"]
