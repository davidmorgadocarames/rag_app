"""Role -> permission map (T12.3.2b, user idea 2026-11-09): routes declare a **permission**,
never a role string. ``require_role("admin")`` is a thin alias kept only for role-level (not
data-level) placeholder checks; new protected routes should prefer ``require_permission``
directly.

Roles stay ``user``/``admin`` in 12b (CHECK constraint, migration 0006) -- adding a future
department role (Phase 21: HR, engineering, software...) is one more entry in
``ROLE_PERMISSIONS`` plus a migration, never a route-level code change. A role is never
accepted from the client (the admin bootstrap CLI, ``rag_app.admin``, is the only writer of
``role``) and never cached in anything the client can read back -- the backend re-reads
``user.role`` from the database on every request (``rag_app.sessions.authenticate``).

**Fail closed on an unknown permission (Done-when):** a route that names a permission not
present in ``ROLE_PERMISSIONS`` -- a coding mistake, e.g. a typo, or a permission a future
Phase 21 role needs that was never added here -- denies EVERY role, including admin, rather
than silently granting it (a bug that fails open) or silently denying only some roles (a bug
that is hard to notice). See ``test_authz.py::test_an_unknown_permission_denies_every_role``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from rag_app.api.auth import CurrentUserDep
from rag_app.db.models import User

logger = logging.getLogger("rag_app.authz")


@dataclass(frozen=True)
class ProtectedRoute:
    """One route gated by ``require_permission``/``require_permission_and_mfa`` -- the
    input to the T12.3.6 role x route tampering matrix. Populated by ``_register`` at the
    point each route is DEFINED (not guessed afterwards by walking the app), so a new
    admin route is covered the moment it declares its permission -- see
    ``PROTECTED_ROUTES``."""

    method: str
    path: str
    permission: str
    requires_mfa: bool


# T12.3.6: "generated from the permission map" -- every entry here comes from an ACTUAL
# require_permission/require_permission_and_mfa(..., route=(method, path)) call next to a
# route definition, never a hand-maintained parallel list a new route could fall outside of.
PROTECTED_ROUTES: list[ProtectedRoute] = []


def _register(method: str, path: str, permission: str, *, requires_mfa: bool) -> None:
    PROTECTED_ROUTES.append(ProtectedRoute(method, path, permission, requires_mfa))


# Role -> the permissions it is granted. "user:base" / "admin:read" double as each role's
# own "is this role, at all" marker (used by require_role below) as well as ordinary
# permissions routes can declare directly.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "user": frozenset({"user:base"}),
    "admin": frozenset({"user:base", "admin:read", "admin:users"}),
}

# The union of every permission any role is actually granted -- the "known permissions"
# registry `require_permission` fails closed against.
ALL_PERMISSIONS: frozenset[str] = frozenset().union(*ROLE_PERMISSIONS.values())

# require_role(role) convenience: which permission stands for "is this role, at all".
_ROLE_BASE_PERMISSION: dict[str, str] = {"user": "user:base", "admin": "admin:read"}


def require_permission(
    permission: str, *, route: tuple[str, str] | None = None
) -> Callable[..., User]:
    """FastAPI dependency factory: 403 unless the caller's role is granted ``permission``.

    Deny-by-default (no permission means no access) and fail-closed on an unknown
    permission (see module docstring) -- both checked at REQUEST time, inside the returned
    dependency, so every call is covered, not just the ones exercised when the route was
    defined.

    ``route=(method, path)`` registers this call into ``PROTECTED_ROUTES`` (T12.3.6) --
    pass it from every real route (not from a throwaway dependency built only for a test).
    """
    if permission not in ALL_PERMISSIONS:
        logger.warning(
            "require_permission(%r): not in ROLE_PERMISSIONS -- every role is denied", permission
        )
    if route is not None:
        _register(route[0], route[1], permission, requires_mfa=False)

    def _dependency(user: CurrentUserDep) -> User:
        granted = ROLE_PERMISSIONS.get(user.role, frozenset())
        if permission not in ALL_PERMISSIONS or permission not in granted:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return user

    return _dependency


def require_role(role: str, *, route: tuple[str, str] | None = None) -> Callable[..., User]:
    """Thin alias over ``require_permission`` (T12.3.2b): ``require_role("admin")`` is
    exactly ``require_permission("admin:read")``. Kept for role-level placeholder checks
    (``/admin/ping``); a route protecting real admin data should name its own permission."""
    permission = _ROLE_BASE_PERMISSION.get(role)
    if permission is None:
        logger.warning("require_role(%r): not a known role -- every role is denied", role)
        permission = f"__unknown_role__:{role}"
    return require_permission(permission, route=route)


def require_permission_and_mfa(
    permission: str, *, route: tuple[str, str] | None = None
) -> Callable[..., User]:
    """Admin-grade routes (T12.3.5): ``permission`` AND a fresh MFA step-up on THIS session
    (``sessions.mfa_verified_at`` set -- ``rag_app.api.auth.get_current_user`` reads it onto
    ``request.state.mfa_verified_at``). The legacy Bearer (no server-side session) path can
    never satisfy this, by construction -- there is no session row to have stepped up.

    ``base`` is called EXPLICITLY in the body (not composed via a nested ``Depends``) so the
    permission check and the MFA check are both exercised by a single, direct call to the
    returned dependency -- in production (FastAPI resolves ``user: CurrentUserDep`` once and
    reuses it) and in a unit test that calls the dependency directly with a fake user/request.
    """
    base = require_permission(permission)  # route registered below, with requires_mfa=True
    if route is not None:
        _register(route[0], route[1], permission, requires_mfa=True)

    def _dependency(user: CurrentUserDep, http: Request) -> User:
        checked = base(user)
        if getattr(http.state, "mfa_verified_at", None) is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "MFA step-up required")
        return checked

    return _dependency
