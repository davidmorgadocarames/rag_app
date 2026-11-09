"""rag_app.authz (T12.3.2b): the role -> permission map, deny-by-default, and fail-closed
on an unknown permission.

Marked ``db``/``security`` for the same reason as ``test_client_ip.py`` (see its
docstring): these particular tests call the dependency CLOSURES directly (plain Python, no
FastAPI/DB involved) but stay grouped with the rest of ``security-suite``.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException

from rag_app.authz import (
    ALL_PERMISSIONS,
    ROLE_PERMISSIONS,
    require_permission,
    require_permission_and_mfa,
    require_role,
)
from rag_app.db.models import User

pytestmark = [pytest.mark.db, pytest.mark.security]


def _user(role: str) -> User:
    return User(role=role)  # in-memory only; never persisted in these tests


class _FakeState:
    def __init__(self, mfa_verified_at: dt.datetime | None = None) -> None:
        self.mfa_verified_at = mfa_verified_at


class _FakeRequest:
    def __init__(self, mfa_verified_at: dt.datetime | None = None) -> None:
        self.state = _FakeState(mfa_verified_at)


# --- the map itself -------------------------------------------------------------------------


def test_roles_stay_user_and_admin_only() -> None:
    assert set(ROLE_PERMISSIONS) == {"user", "admin"}


def test_admin_is_granted_more_than_user() -> None:
    assert ROLE_PERMISSIONS["user"] < ROLE_PERMISSIONS["admin"]


# --- require_permission: deny-by-default + fail-closed --------------------------------------


def test_an_ordinary_user_is_denied_an_admin_permission() -> None:
    dependency = require_permission("admin:read")
    with pytest.raises(HTTPException) as exc_info:
        dependency(_user("user"))
    assert exc_info.value.status_code == 403


def test_an_admin_is_granted_an_admin_permission() -> None:
    dependency = require_permission("admin:read")
    assert dependency(_user("admin")).role == "admin"


def test_an_unknown_permission_denies_every_role() -> None:
    """Done-when (T12.3.2b): a permission never added to ``ROLE_PERMISSIONS`` -- e.g. a
    future department permission before Phase 21 wires it up -- fails closed for EVERY
    role, including admin, instead of silently granting or denying inconsistently."""
    unknown = "docs:hr"
    assert unknown not in ALL_PERMISSIONS
    dependency = require_permission(unknown)
    for role in ("user", "admin"):
        with pytest.raises(HTTPException) as exc_info:
            dependency(_user(role))
        assert exc_info.value.status_code == 403


def test_a_role_with_no_entry_in_the_map_is_denied() -> None:
    """Defence in depth: even if a row ever existed with an unrecognised ``role`` value
    (the CHECK constraint should already prevent this), the permission check still denies
    rather than raising or defaulting to a grant."""
    dependency = require_permission("user:base")
    with pytest.raises(HTTPException):
        dependency(_user("some-future-role-not-yet-in-the-map"))


# --- require_role: a thin alias --------------------------------------------------------------


def test_require_role_admin_behaves_like_require_permission_admin_read() -> None:
    via_role = require_role("admin")
    via_permission = require_permission("admin:read")
    assert via_role(_user("admin")).role == via_permission(_user("admin")).role
    with pytest.raises(HTTPException):
        via_role(_user("user"))
    with pytest.raises(HTTPException):
        via_permission(_user("user"))


def test_require_role_of_an_unknown_role_name_fails_closed() -> None:
    dependency = require_role("superadmin")  # never a real role
    with pytest.raises(HTTPException):
        dependency(_user("admin"))
    with pytest.raises(HTTPException):
        dependency(_user("user"))


# --- require_permission_and_mfa: permission AND a fresh step-up -----------------------------


def test_admin_without_mfa_stepup_is_denied() -> None:
    dependency = require_permission_and_mfa("admin:read")
    with pytest.raises(HTTPException) as exc_info:
        dependency(user=_user("admin"), http=_FakeRequest(mfa_verified_at=None))
    assert exc_info.value.status_code == 403


def test_admin_with_mfa_stepup_is_granted() -> None:
    dependency = require_permission_and_mfa("admin:read")
    result = dependency(
        user=_user("admin"), http=_FakeRequest(mfa_verified_at=dt.datetime.now(dt.UTC))
    )
    assert result.role == "admin"


def test_a_user_is_denied_even_with_an_mfa_stepup_timestamp() -> None:
    """The permission check runs first: an ordinary user can never satisfy this dependency,
    whatever ``request.state.mfa_verified_at`` happens to hold."""
    dependency = require_permission_and_mfa("admin:read")
    with pytest.raises(HTTPException) as exc_info:
        dependency(user=_user("user"), http=_FakeRequest(mfa_verified_at=dt.datetime.now(dt.UTC)))
    assert exc_info.value.status_code == 403
