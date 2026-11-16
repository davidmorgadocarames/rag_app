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
    CatalogueProbeError,
    catalogue_probes,
    git_sha,
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
    return DefenceRunsResponse(
        runs=[
            DefenceRunOut(
                probe_id=row.probe_id,
                git_sha=row.git_sha,
                expected_outcome=row.expected_outcome,
                observed_outcome=row.observed_outcome,
                passed=row.passed,
                run_at=row.run_at,
            )
            for row in rows
        ]
    )
