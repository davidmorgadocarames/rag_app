"""``registry`` gate step (T13.0.5): the feature registry (``rag_app.features``) stays true
to the real app.

Always runs, with or without a live stack (DATABASE_URL is the gate's — a stopped port in
``--fast``, a real one once ``--full``/``--only registry,...`` has brought the stack up):

1. structural checks (``rag_app.features.check_registry``) — every route tag claimed, every
   declared metric exists in the Prometheus registry, every module importable, every
   ``expected-fail`` entry names a registered quality probe. A brand-new router with an
   unclaimed tag fails here.
2. every entry's health probe, each under its own ``≤ 2 s`` deadline (never a build-time
   failure if the stack happens to be down — a probe that returns ``down`` gracefully is
   fine; one that cannot even ANSWER within 2 s is a FAIL).
3. every registered quality probe, with a real DB session when one is reachable (``--full``);
   an ``expected-fail`` entry whose probe now PASSES is a FAIL (the registry must be updated,
   not silently left stale).

    python -m rag_app.devtools.registry_check [--root REPO]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rag_app.features import QUALITY_PROBES, REGISTRY, check_registry, run_health_probe

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

_CONNECT_TIMEOUT_SECONDS = 2


def _open_session() -> tuple[Session | None, Engine | None]:
    """A short-lived session for the quality-probe check, or ``(None, None)`` when the DB is
    unreachable within the same short ``connect_timeout`` every health probe uses (T13.0.4) —
    never blocks this step waiting for a DB that, in ``--fast``, is deliberately not there."""
    from sqlalchemy import create_engine, text

    from rag_app.config import get_job_settings
    from rag_app.db.session import make_session_factory

    url = get_job_settings().database_url
    engine = create_engine(
        url,
        future=True,
        hide_parameters=True,
        connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - no live stack: the normal fast-gate case
        engine.dispose()
        return None, None
    return make_session_factory(engine)(), engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Feature registry structure + health probes + expected-fail check (T13.0.5)."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())  # unused today; kept for
    # consistency with the other devtools' CLI shape.
    parser.parse_args(argv)

    from rag_app.api.app import create_app
    from rag_app.metrics import REGISTRY as PROM_REGISTRY

    app = create_app()
    problems = check_registry(app, PROM_REGISTRY)

    print(f"registry: {len(REGISTRY)} entries")
    not_ok = 0
    for entry in REGISTRY:
        if entry.health_probe is None:
            continue
        health_result = run_health_probe(entry.health_probe)
        print(f"  probe {entry.id}: {health_result.status} — {health_result.detail}")
        if health_result.status != "ok":
            not_ok += 1
        if "timed out" in health_result.detail:
            problems.append(f"{entry.id}: health probe {health_result.detail} (must answer <= 2s)")
    print(f"registry: {not_ok} health probe(s) not 'ok' (expected whenever no stack is up)")

    session, engine = _open_session()
    print(f"registry: quality-probe DB session {'available' if session else 'unavailable'}")
    try:
        for entry in REGISTRY:
            for probe_id in entry.quality_probes:
                probe = QUALITY_PROBES.get(probe_id)
                if probe is None:
                    continue  # already reported by check_registry
                quality_result = probe(session)
                print(
                    f"  quality probe '{probe_id}' ({entry.id}): {quality_result.outcome} —"
                    f" {quality_result.detail}"
                )
                if entry.status == "expected-fail" and quality_result.outcome == "passed":
                    problems.append(
                        f"{entry.id}: status is 'expected-fail' but quality probe '{probe_id}'"
                        " now PASSES — update the registry status"
                    )
    finally:
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()

    for problem in problems:
        print(f"  FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
