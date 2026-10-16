"""Security test suite (T12.1.0): the `security-suite` gate step/CI job runs every test under
this package. All of them are marked ``db`` (they need the Postgres harness) *and*
``security`` — the second marker is how ``gate.sh``/CI tell this directory apart from the
plain `db-tests` step/job so a test here never runs twice (see conftest.py)."""
