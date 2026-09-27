"""The test DB harness refuses the development database (T11.0.14) — no DB needed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from db_harness import HarnessRefusal, check_admin_url

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://rag:rag@localhost:5432/rag",  # .env.example / dev compose
        "postgresql://rag:rag@localhost/rag",  # default port = 5432
        "postgresql://u:p@127.0.0.1:5432/postgres",  # dev server, any database
        "postgresql://u:p@127.0.0.1:55432/rag",  # database named like dev
        "postgresql://u:p@secrag-db.postgres.database.azure.com:5432/rag",  # Azure
        "postgresql://u:p@db.example.com:55432/postgres",  # any remote host
        "sqlite:///tmp/x.db",
        "not a url",
    ],
)
def test_dev_or_remote_urls_are_refused(url: str) -> None:
    with pytest.raises(HarnessRefusal):
        check_admin_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://secrag_gate:pw@127.0.0.1:15432/secrag_gate",  # gate project
        "postgresql+psycopg://postgres:pw@localhost:15432/postgres",  # CI service
    ],
)
def test_gate_and_ci_urls_are_accepted(url: str) -> None:
    assert check_admin_url(url).drivername == "postgresql+psycopg"


def test_run_pointed_at_the_dev_url_aborts_before_any_test() -> None:
    """Row 7b Done-when: a test pointed at the dev URL aborts (no connection attempted)."""
    env = {
        **os.environ,
        "TEST_DATABASE_URL": "postgresql+psycopg://rag:rag@localhost:5432/rag",
        "SECRAG_REQUIRE_DB_TESTS": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_db_schema.py"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 4, output
    assert "refusing port 5432" in output
    assert "passed" not in output


def test_required_db_tests_without_a_url_fail_instead_of_skipping() -> None:
    env = {k: v for k, v in os.environ.items() if k != "TEST_DATABASE_URL"}
    env["SECRAG_REQUIRE_DB_TESTS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_db_schema.py"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "SECRAG_REQUIRE_DB_TESTS=1" in proc.stdout + proc.stderr
