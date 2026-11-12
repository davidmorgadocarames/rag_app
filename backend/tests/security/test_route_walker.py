"""Route walker (T12.5.2, user idea 2026-11-09, DEC-023): every route the backend serves
either (a) declares auth (``rag_app.api.auth.get_current_user``, directly or transitively
through ``rag_app.authz.require_permission``/``require_role``/``require_permission_and_mfa``)
+ a **permission** (``rag_app.authz.DECLARED_PERMISSION_ROUTES`` -- never a role string
comparison, T12.3.2b) + a rate limit (``rag_app.api.deps.RATE_LIMIT_DEPENDENCIES``), or
(b) is on ``PUBLIC_ALLOWLIST`` below -- a CLOSED set, named route by route, never a prefix
or pattern match. A new route that is neither is caught here (fails closed): the test below
proves this against a toy route built the same way, so a future author cannot silently widen
the allow-list by accident and still have this test pass.

Why ``DECLARED_PERMISSION_ROUTES`` and not ``rag_app.authz.PROTECTED_ROUTES`` (T12.3.6):
``PROTECTED_ROUTES`` is deliberately narrower (admin-grade routes a BARE probe -- no body, no
CSRF token -- can safely exercise, feeding the T12.3.6 role x route tampering matrix); an
ordinary route (chat, conversations, MFA enrol/confirm/step-up) still declares its permission
(``require_permission(..., matrix=False)``, or -- only for the two routes living in
``rag_app.api.auth`` itself, which cannot import this module without a cycle --
``rag_app.authz.declare_permission``), but stays out of that matrix. The walker checks the
broader, always-populated registry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from rag_app.api.app import create_app
from rag_app.api.auth import get_current_user
from rag_app.api.deps import RATE_LIMIT_DEPENDENCIES
from rag_app.authz import DECLARED_PERMISSION_ROUTES

pytestmark = pytest.mark.security

# Every (method, path) NOT in here must declare auth + permission + rate limit. A CLOSED
# set (DEC-023): a genuinely public route is added here on purpose, by exact name -- never
# matched by a wildcard/prefix, so a new route always starts "undeclared" until someone
# looks at it. ``/docs``/``/redoc``/``/openapi.json`` only exist with ``ENV=dev`` (T12.2.4);
# listed here regardless since the walker runs against ``create_app()`` without forcing
# ``ENV`` either way.
PUBLIC_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("POST", "/auth/register"),
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/verify"),
        ("POST", "/auth/resend-verification"),
        ("POST", "/auth/forgot-password"),
        ("POST", "/auth/reset"),
        ("POST", "/csp-report"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
    }
)

_SKIPPED_METHODS = {"HEAD", "OPTIONS"}


def _collect_calls(dependant: Any) -> set[Callable[..., Any]]:
    """Every callable in a route's FULL recursive dependency tree -- sub-dependencies
    (``Depends(...)`` used as another dependency's own parameter, e.g.
    ``require_permission``'s inner function depending on ``CurrentUserDep``) included, not
    just the ones listed directly in the route's own ``dependencies=[...]``."""
    calls: set[Callable[..., Any]] = set()
    if dependant.call is not None:
        calls.add(dependant.call)
    for sub in dependant.dependencies:
        calls |= _collect_calls(sub)
    return calls


def _problems(method: str, path: str, calls: set[Callable[..., Any]]) -> list[str]:
    """The walker's actual rule, factored out so the test below can prove it has teeth
    against a toy route, not just pass trivially against the real app."""
    problems = []
    if get_current_user not in calls:
        problems.append("no auth dependency (get_current_user, directly or transitively)")
    if (method, path) not in DECLARED_PERMISSION_ROUTES:
        problems.append("no permission declared (rag_app.authz.DECLARED_PERMISSION_ROUTES)")
    if not calls & RATE_LIMIT_DEPENDENCIES:
        problems.append("no rate limit dependency (rag_app.api.deps.RATE_LIMIT_DEPENDENCIES)")
    return problems


def _non_public_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Every (method, path, route) the walker actually has to check -- not on
    ``PUBLIC_ALLOWLIST``, HEAD/OPTIONS excluded (Starlette's own auto-added methods, not a
    real route declaration)."""
    found: list[tuple[str, str, APIRoute]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in (route.methods or set()) - _SKIPPED_METHODS:
            if (method, route.path) not in PUBLIC_ALLOWLIST:
                found.append((method, route.path, route))
    return found


def _walk(app: FastAPI) -> dict[tuple[str, str], list[str]]:
    """Every non-public route -> its problems (empty list = compliant)."""
    failures: dict[tuple[str, str], list[str]] = {}
    for method, path, route in _non_public_routes(app):
        problems = _problems(method, path, _collect_calls(route.dependant))
        if problems:
            failures[(method, path)] = problems
    return failures


def test_every_route_declares_auth_and_permission_and_a_rate_limit_or_is_public() -> None:
    app = create_app()
    checked = _non_public_routes(app)
    assert checked, "the walker found nothing to check -- did route discovery break?"
    failures = _walk(app)
    assert not failures, "\n".join(f"{m} {p}: {', '.join(ps)}" for (m, p), ps in failures.items())


def test_an_undeclared_route_fails_the_walker() -> None:
    """Proves the walker is not a no-op: a toy route with no auth, no permission and no
    rate limit, not on the allow-list, is caught -- the SAME ``_problems``/``_walk`` logic
    the test above relies on."""
    app = FastAPI()

    @app.get("/__walker_probe__")
    def probe() -> dict[str, str]:
        return {"detail": "undeclared"}

    failures = _walk(app)
    assert ("GET", "/__walker_probe__") in failures
    reported = failures[("GET", "/__walker_probe__")]
    assert len(reported) == 3  # auth + permission + rate limit, all three missing


def test_a_route_with_only_auth_still_fails_for_the_missing_permission_and_rate_limit() -> None:
    """A partial fix (auth declared, nothing else) still fails -- proves each of the three
    checks is independently enforced, not just "is something declared at all"."""
    app = FastAPI()

    @app.get("/__walker_probe_authed__")
    def probe(user: Annotated[Any, Depends(get_current_user)]) -> dict[str, str]:
        return {"detail": "half-declared"}

    failures = _walk(app)
    reported = failures[("GET", "/__walker_probe_authed__")]
    assert len(reported) == 2
    assert all("auth dependency" not in p for p in reported)
