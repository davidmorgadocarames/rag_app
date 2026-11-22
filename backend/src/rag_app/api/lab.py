"""Defence lab admin routes (T13.3.1/T13.3.5, 13-D, local only -- DEC-028).

Registered by ``rag_app.api.app.create_app`` ONLY when ``settings.lab_enabled`` is true
(T13.3.1) -- with the default ``False``, or with ``ENV=prod`` (the start-up guard refuses to
serve at all regardless), these routes simply do not exist: a request to them is a 404, not
a 403. Every route still goes through the SAME permission map + MFA step-up + rate limit +
CSRF conventions as the rest of ``/admin/*`` (``rag_app.api.admin``), and every run is logged
in ``admin_actions`` (T13.1.6's audit trail) -- this is an admin-only feature, not a public
diagnostic.

``POST /admin/lab/run`` triggers the SAME catalogue-probe code the ``defence-suite`` gate
step (``rag_app.devtools.defence_lab_cli``) and the CLI use (``rag_app.defence_lab`` --
"one implementation", T13.3.5) and runs it IN THE REQUEST (blocking): the curated probe set
is small and each test is quick once collected, well inside the BFF's >= 240 s upstream
timeout (DEC-006). See ``rag_app.defence_lab``'s module docstring for the "fixed local
target" requirement (``TEST_DATABASE_URL`` must be exported on this process first) -- an
unset target is reported back as a clear 503, never a silent pass.

``POST /admin/lab/run/{probe_id}`` (T13.J4-fix, DA-13-J3J4-1) is the SAME run, scoped to
exactly one catalogue probe -- the mockup's own per-card "Run probe" button, which the
13-J3 panel used to wire to the whole-lab route above instead (every card refreshed, not
just the one pressed).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from rag_app.admin_audit import record_admin_action
from rag_app.api.auth import CurrentUserDep
from rag_app.api.deps import SessionDep, rate_limit_account
from rag_app.api.schemas import (
    DefenceLabRunResponse,
    DefenceProbeResultOut,
    DefenceRunOut,
    DefenceRunsResponse,
)
from rag_app.authz import require_permission_and_mfa
from rag_app.csrf import require_csrf
from rag_app.db.models import DefenceRun
from rag_app.defence_lab import (
    CATALOGUE_PROBE_IDS,
    CatalogueProbeError,
    catalogue_probes,
    git_sha,
    probe_display_info,
    record_results,
    run_catalogue_probes,
)

router = APIRouter()


@router.post(
    "/admin/lab/run",
    response_model=DefenceLabRunResponse,
    tags=["admin-lab"],
    dependencies=[
        Depends(require_csrf),
        # matrix=False (DA-13-D-2): unlike the other matrix=False admin routes this one needs
        # no path id or body; it is excluded because an allowed matrix probe would start a
        # real, slow `pytest` subprocess that re-runs the security suite from inside it.
        # Still covered by the route walker + the explicit tests in tests/security/test_lab_api.py.
        Depends(
            require_permission_and_mfa(
                "admin:users", route=("POST", "/admin/lab/run"), matrix=False
            )
        ),
        Depends(rate_limit_account("admin_lab_run")),
    ],
)
def admin_lab_run(admin: CurrentUserDep, session: SessionDep) -> DefenceLabRunResponse:
    """T13.3.5: runs every catalogue probe, records the results (T13.3.4), logs an
    ``admin_actions`` row (never the probe evidence -- only the run outcome, same "opaque
    target" convention as every other mutating admin route)."""
    try:
        probes = catalogue_probes()
    except CatalogueProbeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    sha = git_sha()
    results = run_catalogue_probes(probes)
    all_matched = all(r.outcome.passed for r in results)
    record_results(session, results, sha=sha)
    record_admin_action(
        session,
        admin.id,
        "defence_lab_run",
        target=f"{len(results)} probes @ {sha}",
        result="all_matched" if all_matched else "mismatch",
    )
    session.commit()
    return DefenceLabRunResponse(
        detail="Defence lab run complete." if all_matched else "One or more probes did not match.",
        git_sha=sha,
        results=[
            DefenceProbeResultOut(
                probe_id=r.probe.id,
                expected_outcome=r.probe.expected_outcome,
                observed_outcome=r.outcome.observed,
                passed=r.outcome.passed,
                name=r.probe.name,
                description=r.probe.description,
            )
            for r in results
        ],
        all_matched=all_matched,
    )


@router.post(
    "/admin/lab/run/{probe_id}",
    response_model=DefenceLabRunResponse,
    tags=["admin-lab"],
    dependencies=[
        Depends(require_csrf),
        # matrix=False, same reasoning as POST /admin/lab/run above: this path is
        # parametrised (the T12.3.6 bare-probe tampering matrix has no placeholder value to
        # substitute for {probe_id} and would 404 on the literal "{probe_id}" segment) and,
        # like the whole-lab route, starts a real pytest subprocess the matrix must never
        # trigger. Still covered by the route walker (DECLARED_PERMISSION_ROUTES) and the
        # explicit tests in tests/security/test_lab_api.py.
        Depends(
            require_permission_and_mfa(
                "admin:users", route=("POST", "/admin/lab/run/{probe_id}"), matrix=False
            )
        ),
        Depends(rate_limit_account("admin_lab_run")),
    ],
)
def admin_lab_run_one(
    probe_id: str, admin: CurrentUserDep, session: SessionDep
) -> DefenceLabRunResponse:
    """T13.J4-fix (DA-13-J3J4-1): runs exactly ONE catalogue probe -- the mockup's own
    per-card "Run probe" button, which the 13-J3 panel used to wire to the whole-lab route
    above (every card's badge/outcome/timestamp refreshed, not just the one pressed). Same
    gating as the whole-lab run (CSRF, step-up MFA, ``admin:users``, dev-only via
    ``LAB_ENABLED``, since this router is only mounted when the lab is on at all). A
    ``probe_id`` that is not one of the lab's own catalogue ids is a 404 -- never a 500/422,
    the id is a path segment to validate against ``CATALOGUE_PROBE_IDS``, not a body shape.
    """
    if probe_id not in CATALOGUE_PROBE_IDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{probe_id}' is not a defence-lab probe")
    try:
        probes = catalogue_probes([probe_id])
    except CatalogueProbeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    sha = git_sha()
    results = run_catalogue_probes(probes)
    all_matched = all(r.outcome.passed for r in results)
    record_results(session, results, sha=sha)
    record_admin_action(
        session,
        admin.id,
        "defence_lab_run",
        target=f"1 probe ({probe_id}) @ {sha}",
        result="all_matched" if all_matched else "mismatch",
    )
    session.commit()
    return DefenceLabRunResponse(
        detail="Defence lab run complete." if all_matched else "One or more probes did not match.",
        git_sha=sha,
        results=[
            DefenceProbeResultOut(
                probe_id=r.probe.id,
                expected_outcome=r.probe.expected_outcome,
                observed_outcome=r.outcome.observed,
                passed=r.outcome.passed,
                name=r.probe.name,
                description=r.probe.description,
            )
            for r in results
        ],
        all_matched=all_matched,
    )


@router.get(
    "/admin/lab/runs",
    response_model=DefenceRunsResponse,
    tags=["admin-lab"],
    dependencies=[
        Depends(require_permission_and_mfa("admin:read", route=("GET", "/admin/lab/runs"))),
        Depends(rate_limit_account("admin_lab_runs")),
    ],
)
def admin_lab_runs(session: SessionDep) -> DefenceRunsResponse:
    """History (T13.3.4): the most recent probe results, newest first -- "trends per phase"
    means comparing the ``git_sha`` of successive runs, left to a future dashboard view."""
    from sqlalchemy import select

    rows = session.scalars(select(DefenceRun).order_by(DefenceRun.run_at.desc()).limit(200)).all()
    # T13.J4-fix (DA-13-J3J4-2): a HISTORY row only ever stores a bare `probe_id` -- look up
    # every distinct id's (name, description) ONCE (a single catalogue parse), not once per
    # row.
    display = probe_display_info(sorted({row.probe_id for row in rows}))
    return DefenceRunsResponse(
        runs=[
            DefenceRunOut(
                probe_id=row.probe_id,
                git_sha=row.git_sha,
                expected_outcome=row.expected_outcome,
                observed_outcome=row.observed_outcome,
                passed=row.passed,
                run_at=row.run_at,
                name=display[row.probe_id][0],
                description=display[row.probe_id][1],
            )
            for row in rows
        ]
    )
