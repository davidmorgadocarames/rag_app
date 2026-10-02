"""Fail-fast settings validation in the API lifespan only; Jobs use JobSettings (T11.2.2);
master-key fingerprint helper (T11.2.4, unit part — the DB part is test_startup_db.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rag_app.api.app import create_app
from rag_app.config import (
    DEV_DATABASE_URL,
    JobSettings,
    Settings,
    SettingsValidationError,
    get_job_settings,
    validate_api_settings,
)
from rag_app.keycheck import fingerprint

BACKEND = Path(__file__).resolve().parents[1]
SECRET_ENV = ("JWT_SECRET", "DATA_MASTER_KEY", "DATABASE_URL", "ENV")
GOOD_URL = "postgresql+psycopg://user:pw-not-echoed@127.0.0.1:15432/startup_unit"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: str) -> Settings:
    values = {
        "database_url": GOOD_URL,
        "jwt_secret": "j" * 32,
        "data_master_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_valid_settings_pass() -> None:
    validate_api_settings(_settings())


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"jwt_secret": "short-secret-value"}, "JWT_SECRET must be at least 32 characters"),
        ({"jwt_secret": ""}, "JWT_SECRET must be at least 32 characters"),
        ({"data_master_key": "not-a-fernet-key-SECRETVALUE"}, "DATA_MASTER_KEY is not a valid"),
        ({"data_master_key": ""}, "DATA_MASTER_KEY is not a valid"),
        ({"database_url": "mysql://u:pw-not-echoed@h/db"}, "must be a PostgreSQL URL"),
        ({"database_url": "not a url pw-not-echoed"}, "DATABASE_URL is not a database URL"),
        ({"database_url": "postgresql://u:pw-not-echoed@h:5432"}, "names no database"),
        ({"database_url": "   "}, "DATABASE_URL is empty"),
    ],
)
def test_each_invalid_setting_is_named_without_its_value(
    overrides: dict[str, str], expected: str
) -> None:
    with pytest.raises(SettingsValidationError) as info:
        validate_api_settings(_settings(**overrides))
    message = str(info.value)
    assert expected in message
    for value in overrides.values():
        if value.strip():
            assert value not in message
    assert "pw-not-echoed" not in message and "SECRETVALUE" not in message


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(SettingsValidationError) as info:
        validate_api_settings(_settings(jwt_secret="x", data_master_key="y"))
    assert "JWT_SECRET" in str(info.value) and "DATA_MASTER_KEY" in str(info.value)


def test_prod_refuses_the_development_database_default() -> None:
    settings = Settings(
        _env_file=None, jwt_secret="j" * 32, data_master_key=Fernet.generate_key().decode()
    )
    assert settings.env == "prod" and settings.database_url == DEV_DATABASE_URL
    with pytest.raises(SettingsValidationError, match="DATABASE_URL is not set"):
        validate_api_settings(settings)


def test_dev_accepts_the_development_database_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    settings = Settings(
        _env_file=None, jwt_secret="j" * 32, data_master_key=Fernet.generate_key().decode()
    )
    validate_api_settings(settings)


def test_database_url_from_the_environment_counts_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", GOOD_URL)
    monkeypatch.setenv("JWT_SECRET", "j" * 32)
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    validate_api_settings(Settings(_env_file=None))


def test_settings_construction_never_fails_on_missing_secrets() -> None:
    """Validation lives in the lifespan only: importing/constructing settings is harmless."""
    settings = Settings(_env_file=None)
    assert settings.jwt_secret == "" and settings.data_master_key == ""


def test_the_api_lifespan_refuses_invalid_settings_before_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("rag_app.api.app.get_settings", lambda: _settings(jwt_secret="short"))
    monkeypatch.setattr(
        "rag_app.api.app.check_master_key_fingerprint", lambda *a: calls.append(a) or "match"
    )
    with pytest.raises(SettingsValidationError), TestClient(create_app()):
        pass
    assert calls == []


def test_building_the_app_does_not_validate() -> None:
    """Without `with`, no lifespan runs: tests and tools may import/create the app freely."""
    client = TestClient(create_app())
    assert client.get("/health").status_code == 200


def test_the_api_lifespan_warms_up_the_shared_reranker(monkeypatch: pytest.MonkeyPatch) -> None:
    """T11.4.1: the lifespan loads and warms up the ONE shared reranker before the app starts
    serving traffic — so the first real chat request never pays the model-load/first-
    inference cost (that cost is paid here, once, at start-up instead)."""
    from rag_app.api import app as app_module

    calls: list[object] = []

    class _FakeReranker:
        def warm_up(self) -> None:
            calls.append(True)

    monkeypatch.setattr(app_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(app_module, "check_master_key_fingerprint", lambda *_a: "match")
    monkeypatch.setattr(app_module.reranking, "get_shared_reranker", lambda: _FakeReranker())
    with TestClient(create_app()):
        pass
    assert calls == [True]


def test_metrics_bind_addr_defaults_to_all_interfaces_but_is_configurable() -> None:
    """DA-11bA-2: compose's separate `prometheus` container needs a non-loopback bind to
    reach this port at all, so the default stays "0.0.0.0" — but it must be overridable
    (e.g. to "127.0.0.1" on Azure, where nothing scrapes it remotely today) without a code
    change, since ingress is configured once at `az containerapp create` time and never by
    this setting."""
    assert _settings().metrics_bind_addr == "0.0.0.0"
    assert _settings(metrics_bind_addr="127.0.0.1").metrics_bind_addr == "127.0.0.1"


def test_the_api_lifespan_passes_the_configured_bind_addr_to_the_metrics_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_app.api import app as app_module

    calls: list[tuple[int, str]] = []

    class _NoopReranker:
        def warm_up(self) -> None:
            pass

    def _fake_start(port: int, addr: str = "0.0.0.0") -> int:
        calls.append((port, addr))
        return port

    monkeypatch.setattr(
        app_module, "get_settings", lambda: _settings(metrics_bind_addr="127.0.0.1")
    )
    monkeypatch.setattr(app_module, "check_master_key_fingerprint", lambda *_a: "match")
    monkeypatch.setattr(app_module.metrics, "start_metrics_server", _fake_start)
    monkeypatch.setattr(app_module.reranking, "get_shared_reranker", lambda: _NoopReranker())
    with TestClient(create_app()):
        pass
    assert calls == [(9100, "127.0.0.1")]


def test_job_settings_have_no_api_secrets() -> None:
    fields = set(JobSettings.model_fields)
    assert fields == {"env", "database_url"}
    assert not {"jwt_secret", "data_master_key"} & fields
    assert JobSettings(_env_file=None).database_url == DEV_DATABASE_URL
    assert isinstance(get_job_settings(), JobSettings)


def test_a_job_starts_without_jwt_secret_or_master_key(tmp_path: Path) -> None:
    """A Job process (no JWT_SECRET/DATA_MASTER_KEY in its environment, no .env) imports the
    Job modules and builds its engine; the migrations env reads JobSettings only."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["PYTHONPATH"] = str(BACKEND / "src")
    env["DATABASE_URL"] = GOOD_URL
    code = (
        "import rag_app.erasure, rag_app.keycheck\n"
        "from rag_app.config import get_job_settings\n"
        "from rag_app.db.session import make_engine\n"
        "s = get_job_settings()\n"
        "e = make_engine()\n"
        "assert e.url.database == 'startup_unit', e.url.database\n"
        "assert not hasattr(s, 'jwt_secret') and not hasattr(s, 'data_master_key')\n"
        "print('job-ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "job-ok"
    migrations_env = (BACKEND / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "get_job_settings" in migrations_env and "get_settings()" not in migrations_env


def test_fingerprint_is_stable_distinct_and_does_not_contain_the_key() -> None:
    a, b = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    assert fingerprint(a) == fingerprint(a)
    assert fingerprint(a) != fingerprint(b)
    assert len(fingerprint(a)) == 64
    assert a not in fingerprint(a) and a.rstrip("=")[:16] not in fingerprint(a)
