"""``defence-suite`` gate step (T13.3.6): runs every catalogue-driven defence probe
(``rag_app.defence_lab``), records the results into ``defence_runs`` (T13.3.4) and fails
unless 100% of probes match their expected outcome (T13.3.6's own Done-when).

    python -m rag_app.devtools.defence_lab_cli run [--ids ID [ID ...]]

Needs the SAME environment as ``security-suite``/``db-tests`` (``TEST_DATABASE_URL`` for the
catalogue probes' own test-DB harness, ``DATABASE_URL`` for the persistent ``defence_runs``
history this writes) -- see ``rag_app.defence_lab``'s module docstring for the full
"fixed local target" explanation.
"""

from __future__ import annotations

import argparse
import sys

from rag_app.defence_lab import (
    CATALOGUE_PROBE_IDS,
    CatalogueProbeError,
    catalogue_probes,
    git_sha,
    record_results,
    run_catalogue_probes,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.devtools.defence_lab_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_parser = sub.add_parser("run", help="runs every catalogue probe and records the results")
    run_parser.add_argument("--ids", nargs="+", default=list(CATALOGUE_PROBE_IDS))
    args = parser.parse_args(argv)

    if args.cmd != "run":  # pragma: no cover - argparse already enforces this
        return 1

    try:
        probes = catalogue_probes(args.ids)
    except CatalogueProbeError as exc:
        print(f"defence-suite: FAIL — {exc}", file=sys.stderr)
        return 1

    sha = git_sha()
    results = run_catalogue_probes(probes)

    from rag_app.db.session import make_engine, make_session_factory

    engine = make_engine()
    try:
        session = make_session_factory(engine)()
        try:
            record_results(session, results, sha=sha)
            session.commit()
        finally:
            session.close()
    finally:
        engine.dispose()

    failed = [r for r in results if not r.outcome.passed]
    print(f"defence-suite: {len(results)} probe(s) at {sha}, {len(failed)} not matching expected")
    for result in results:
        mark = "ok" if result.outcome.passed else "FAIL"
        print(
            f"  {mark} {result.probe.id}: {result.outcome.observed}"
            f" — {result.probe.expected_outcome}"
        )
    if failed:
        print(
            f"defence-suite: FAIL — {len(failed)}/{len(results)} probe(s) did not match",
            file=sys.stderr,
        )
        return 1
    print(f"defence-suite: PASS — {len(results)}/{len(results)} probe(s) matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
