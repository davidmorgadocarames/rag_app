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
# Scoped to routes the T12.3.6 tampering matrix can safely probe with a bare request (no
# body, no CSRF token) -- admin-grade routes today. An ordinary authenticated route (chat,
# conversations, MFA enrol/confirm/step-up...) declares its permission with ``matrix=False``
# (still required -- see ``DECLARED_PERMISSION_ROUTES`` below) because a bare probe of it
# would 422/403 on body/CSRF grounds that have nothing to do with the permission check.
PROTECTED_ROUTES: list[ProtectedRoute] = []

# T12.5.2 (route walker): EVERY route that declares a permission through
# ``require_permission``/``require_role``/``require_permission_and_mfa`` with ``route=...``,
# admin-grade or not -- the walker (backend/tests/security/test_route_walker.py) checks
# membership here, never ``PROTECTED_ROUTES`` (kept narrower, see above).
DECLARED_PERMISSION_ROUTES: dict[tuple[str, str], str] = {}


def _register(method: str, path: str, permission: str, *, requires_mfa: bool, matrix: bool) -> None:
    DECLARED_PERMISSION_ROUTES[(method, path)] = permission
    if matrix:
        PROTECTED_ROUTES.append(ProtectedRoute(method, path, permission, requires_mfa))


def declare_permission(method: str, path: str, permission: str) -> None:
    """T12.5.2 (route walker): records that ``method path`` requires ``permission`` WITHOUT
    building a FastAPI dependency -- for a route whose own existing dependency (``rag_app.
    api.auth.CurrentUserDep``) already enforces authentication, and whose permission is
    ``user:base`` (granted to every role that exists today): there is nothing left to
    decide at request time, only to register for the walker.

    Only ``rag_app.api.auth``'s own routes (``/auth/me``, ``DELETE /account``) use this --
    everywhere else, call ``require_permission(..., matrix=False)`` as a real dependency
    instead (``rag_app.api.conversations``, ``rag_app.api.app``). ``rag_app.api.auth``
    cannot import THIS module at all: ``require_permission``'s own dependency needs
    ``CurrentUserDep``, which lives in ``rag_app.api.auth`` -- importing this module FROM
    there would be a cycle (auth -> authz -> auth). ``rag_app.api.app`` calls this once,
    right after it includes ``auth.router``, instead."""
    if permission not in ALL_PERMISSIONS:
        logger.warning(
            "declare_permission(%r): not in ROLE_PERMISSIONS -- every role is denied", permission
        )
    DECLARED_PERMISSION_ROUTES[(method, path)] = permission


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
    permission: str, *, route: tuple[str, str] | None = None, matrix: bool = True
) -> Callable[..., User]:
    """FastAPI dependency factory: 403 unless the caller's role is granted ``permission``.

    Deny-by-default (no permission means no access) and fail-closed on an unknown
    permission (see module docstring) -- both checked at REQUEST time, inside the returned
    dependency, so every call is covered, not just the ones exercised when the route was
    defined.

    ``route=(method, path)`` registers this call into ``DECLARED_PERMISSION_ROUTES``
    (T12.5.2, the route walker) -- pass it from every real route (not from a throwaway
    dependency built only for a test). ``matrix=False`` (T12.3.6's bare-probe limitation,
    see ``PROTECTED_ROUTES``'s docstring) keeps it OUT of the tampering matrix while still
    registering it for the walker -- pass it for an ordinary route that needs a body and/or
    a CSRF token to call for real (chat, conversations, MFA enrol/confirm/step-up); leave
    the default ``True`` for an admin-grade route a bare GET/empty-POST can safely probe.
    """
    if permission not in ALL_PERMISSIONS:
        logger.warning(
            "require_permission(%r): not in ROLE_PERMISSIONS -- every role is denied", permission
        )
    if route is not None:
        _register(route[0], route[1], permission, requires_mfa=False, matrix=matrix)

    def _dependency(user: CurrentUserDep) -> User:
        granted = ROLE_PERMISSIONS.get(user.role, frozenset())
        if permission not in ALL_PERMISSIONS or permission not in granted:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return user

    return _dependency


def require_role(
    role: str, *, route: tuple[str, str] | None = None, matrix: bool = True
) -> Callable[..., User]:
    """Thin alias over ``require_permission`` (T12.3.2b): ``require_role("admin")`` is
    exactly ``require_permission("admin:read")``. Kept for role-level placeholder checks
    (``/admin/ping``); a route protecting real admin data should name its own permission."""
    permission = _ROLE_BASE_PERMISSION.get(role)
    if permission is None:
        logger.warning("require_role(%r): not a known role -- every role is denied", role)
        permission = f"__unknown_role__:{role}"
    return require_permission(permission, route=route, matrix=matrix)


def require_permission_and_mfa(
    permission: str, *, route: tuple[str, str] | None = None, matrix: bool = True
) -> Callable[..., User]:
    """Admin-grade routes (T12.3.5): ``permission`` AND a fresh MFA step-up on THIS session
    (``sessions.mfa_verified_at`` set -- ``rag_app.api.auth.get_current_user`` reads it onto
    ``request.state.mfa_verified_at``). Cookie authentication is the only path (T12.5.3) --
    no session cookie means no server-side session, so there is nothing to have stepped up.

    ``base`` is called EXPLICITLY in the body (not composed via a nested ``Depends``) so the
    permission check and the MFA check are both exercised by a single, direct call to the
    returned dependency -- in production (FastAPI resolves ``user: CurrentUserDep`` once and
    reuses it) and in a unit test that calls the dependency directly with a fake user/request.
    """
    # No route= on this inner call -- registered below instead, with requires_mfa=True and
    # this call's own matrix= (the inner call's default would register it wrong otherwise).
    base = require_permission(permission)
    if route is not None:
        _register(route[0], route[1], permission, requires_mfa=True, matrix=matrix)

    def _dependency(user: CurrentUserDep, http: Request) -> User:
        checked = base(user)
        if getattr(http.state, "mfa_verified_at", None) is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "MFA step-up required")
        return checked

    return _dependency
