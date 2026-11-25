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
from rag_app.features import iter_routes

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
        # T16.2.1: the k8s readiness probe hits this unauthenticated, same reasoning as
        # /health itself -- a probe never carries a session.
        ("GET", "/health/ready"),
        # T13.13.2 (DEBT-061): the wake ping -- unauthenticated by design (same reasoning as
        # /csp-report below: it must work before any session exists, from /login itself),
        # rate-limited through its own tight bucket (DEC-050, T13.J4-fix:
        # rag_app.api.deps.rate_limit_wake_narrow) and leaks nothing (always a plain 204,
        # see rag_app.wake's docstring). No longer triggers the Ollama warm-up at all.
        ("GET", "/wake"),
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
    real route declaration).

    DA-13-A-1 (blocker, found during 13-A's DA review, pre-existing): this used to iterate
    ``app.routes`` directly with ``isinstance(route, APIRoute)``. With the pinned
    ``fastapi==0.142.2``, a router registered via ``app.include_router(...)`` is NOT inlined
    into ``app.routes`` -- it appears as one opaque ``_IncludedRouter`` wrapper instead, which
    fails that ``isinstance`` check. That silently reduced the walker to checking only the
    two routes ``create_app()`` defines directly on ``app`` itself (``/health``, on the
    allow-list, and ``/chat``) -- 19 of 21 real routes (``/auth/*``, every MFA route,
    ``/admin/ping``, every ``/conversations*`` route, ``DELETE /account``, ``/csp-report``)
    had ZERO walker coverage while the test still reported green, because its only
    self-check (``assert checked``) merely required a non-empty list. Fixed by reusing
    ``rag_app.features.iter_routes`` -- the SAME flattening helper ``check_registry``/
    ``route_probe`` already rely on for this exact FastAPI quirk -- instead of walking
    ``app.routes`` directly."""
    found: list[tuple[str, str, APIRoute]] = []
    for route in iter_routes(app):
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
    # DA-13-A-1: ``assert checked`` alone ("did route discovery break?") is exactly the
    # self-check that stayed green while the walker silently shrank from ~19 checkable
    # routes to 1 (see ``_non_public_routes``'s docstring) -- a non-empty list is not enough
    # to prove discovery is intact. Two independent floors instead, both measured LIVE
    # against today's real app rather than hard-coded, so this can never again pass while
    # seeing only a handful of routes: (1) the number of real ``APIRoute``s
    # ``rag_app.features.iter_routes`` finds -- counted as ``APIRoute`` only (NOT the raw
    # flattened list, which also includes FastAPI's own framework routes, ``/docs``/
    # ``/redoc``/``/openapi.json``/the OAuth2 redirect, registered only with ``ENV=dev``,
    # T12.2.4 -- a hard-coded floor over the raw list is ENV-dependent and would be flaky in
    # CI, which runs with the ``ENV=prod`` default); this alone would not have caught the
    # original bug (an `isinstance` check still ran, just over the wrong, un-flattened list),
    # but IS the same list ``check_registry``/``route_probe`` rely on, so a flattening
    # regression there shows up here too; (2) every permission-declared route
    # (``DECLARED_PERMISSION_ROUTES``, populated independently by route decorators, not by
    # walking ``app.routes`` at all) must actually be among the routes the walker checks --
    # this is the assertion that would have caught DA-13-A-1 (it only held 1 of 12 entries
    # under the old, un-flattened walker).
    api_routes = [route for route in iter_routes(app) if isinstance(route, APIRoute)]
    assert len(api_routes) >= 21, (
        "rag_app.features.iter_routes(app) returned fewer APIRoutes than expected -- route"
        f" discovery may have broken (got {len(api_routes)})"
    )
    checked_keys = {(method, path) for method, path, _route in checked}
    # 13-D (found running security-suite for real, fixed before relying on it): intersect
    # with routes THIS app instance actually mounts first. `DECLARED_PERMISSION_ROUTES` is
    # populated once, process-wide, the moment a route-declaring module is first IMPORTED
    # (the `@router.post(..., dependencies=[Depends(require_permission_and_mfa(..., route=...))
    # ])` decorator runs at module import, not at `include_router()` time) -- a dev-only
    # feature whose router is only conditionally included (`rag_app.api.lab`, T13.3.1) can get
    # imported by an UNRELATED test elsewhere in the same pytest session (module imports
    # never un-import) without ever being mounted on a `create_app()` built with the feature
    # off. Without this intersection, the very first test in a session to import such a
    # module would make EVERY LATER call of this walker test fail with a false "route
    # discovery is vacuous" positive, for a route this app never serves at all.
    app_route_keys = {
        (method, route.path)
        for route in iter_routes(app)
        if isinstance(route, APIRoute)
        for method in (route.methods or set()) - _SKIPPED_METHODS
    }
    missing = (set(DECLARED_PERMISSION_ROUTES) & app_route_keys) - checked_keys
    assert not missing, (
        "the walker never even looked at these permission-declared, actually-mounted routes"
        f" -- route discovery is vacuous again: {sorted(missing)}"
    )
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


def test_a_declared_route_never_mounted_on_this_app_is_not_a_false_positive() -> None:
    """13-D regression: a route another module declared (e.g. a dev-only feature's router,
    imported but not included because the flag is off on THIS app) must never trip the
    "walker discovery is vacuous" floor -- only a declared route this app actually SERVES
    counts. Verified to fail against the PRE-fix formula (``set(DECLARED_PERMISSION_ROUTES)
    - checked_keys``, with no intersection against the app's own routes first)."""
    phantom = ("GET", "/__never_mounted_on_this_app__")
    DECLARED_PERMISSION_ROUTES[phantom] = "some:permission"
    try:
        app = create_app()
        checked_keys = {(method, path) for method, path, _route in _non_public_routes(app)}
        app_route_keys = {
            (method, route.path)
            for route in iter_routes(app)
            if isinstance(route, APIRoute)
            for method in (route.methods or set()) - _SKIPPED_METHODS
        }
        fixed_missing = (set(DECLARED_PERMISSION_ROUTES) & app_route_keys) - checked_keys
        assert phantom not in fixed_missing

        pre_fix_missing = set(DECLARED_PERMISSION_ROUTES) - checked_keys
        assert phantom in pre_fix_missing
    finally:
        del DECLARED_PERMISSION_ROUTES[phantom]


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
