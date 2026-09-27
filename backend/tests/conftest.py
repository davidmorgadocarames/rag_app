"""Shared pytest configuration: the ``db`` marker and the test DB harness fixtures.

See ``db_harness.py``. ``TEST_DATABASE_URL`` unset → ``db`` tests are skipped, unless
``SECRAG_REQUIRE_DB_TESTS=1`` (gate ``--full`` / CI), which turns the skip into an error.
A ``TEST_DATABASE_URL`` that could be the development database aborts the whole run before
any test executes or any connection is made.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from db_harness import (
    HarnessRefusal,
    apply_roles,
    assert_harness_database,
    check_admin_url,
    create_database,
    drop_database,
    migrate,
)

REQUIRE_ENV = "SECRAG_REQUIRE_DB_TESTS"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "db: needs the test DB harness (TEST_DATABASE_URL); skipped in --fast"
    )
    raw = os.environ.get("TEST_DATABASE_URL", "")
    if raw:
        try:
            check_admin_url(raw)
        except HarnessRefusal as exc:
            pytest.exit(f"test DB harness: {exc}", returncode=4)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("TEST_DATABASE_URL"):
        return
    db_items = [item for item in items if item.get_closest_marker("db")]
    if not db_items:
        return
    if os.environ.get(REQUIRE_ENV) == "1":
        raise pytest.UsageError(
            f"{len(db_items)} db tests would be skipped but {REQUIRE_ENV}=1:"
            " set TEST_DATABASE_URL (gate project or CI service)"
        )
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set (db tests run in --full / CI)")
    for item in db_items:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def admin_url() -> URL:
    return check_admin_url(os.environ["TEST_DATABASE_URL"])


@pytest.fixture(scope="session")
def db_url(admin_url: URL) -> Iterator[URL]:
    """A fresh, migrated database created by the harness for this session."""
    url = create_database(admin_url)
    try:
        engine = create_engine(url, future=True)
        try:
            assert_harness_database(engine)
            migrate(url)
            apply_roles(engine)
        finally:
            engine.dispose()
        yield url
    finally:
        drop_database(admin_url, url)


@pytest.fixture()
def db_engine(db_url: URL) -> Iterator[Engine]:
    engine = create_engine(db_url, future=True)
    assert_harness_database(engine)
    try:
        yield engine
    finally:
        engine.dispose()
