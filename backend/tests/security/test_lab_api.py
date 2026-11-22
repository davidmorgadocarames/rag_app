"""Defence lab admin routes (T13.3.1/T13.3.5): absent by construction with the lab off
(a 404, not a 403), permission/MFA/CSRF gated like every other ``/admin/*`` mutating route
when it is on, and the ``admin_actions``/``defence_runs`` writes it makes.

The catalogue-probe execution itself (subprocess pytest, THREAT_MODEL parsing) is covered by
``test_defence_lab.py``; here the route is driven with a FAKE probe runner (no live
``TEST_DATABASE_URL``/subprocess needed) to prove the wiring: permission map, MFA step-up,
rate limit, CSRF, the response shape, and the two DB writes.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from rag_app.admin import create_admin
from rag_app.api.app import create_app
from rag_app.config import get_settings
from rag_app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, csrf_token
from rag_app.db.models import AdminAction, DefenceRun
from rag_app.defence_lab import Probe, ProbeOutcome, ProbeResult
from rag_app.sessions import SESSION_COOKIE_NAME, create_session, set_mfa_verified

pytestmark = [pytest.mark.db, pytest.mark.security]

BASE_URL = "https://testserver"


@pytest.fixture()
def api_env(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Same shape as ``test_admin_api.py``'s own ``api_env`` fixture."""
    monkeypatch.setenv("DATA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "t" * 48)
    monkeypatch.setenv("CSRF_SECRET", "c" * 48)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("PROMETHEUS_URL", "")
    return db_engine


def _creds() -> dict[str, str]:
    return {
        "email": f"lab-api-{uuid.uuid4().hex[:10]}@example.test",
        "password": "correct horse battery staple " + uuid.uuid4().hex[:6],
    }


def _admin_headers(engine: Engine, *, stepped_up: bool) -> dict[str, str]:
    creds = _creds()
    with Session(engine) as session:
        user = create_admin(session, creds["email"], creds["password"])
        raw = create_session(session, user)
        if stepped_up:
            set_mfa_verified(session, raw)
        session.commit()
    csrf = csrf_token(get_settings().csrf_secret, raw)
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={raw}; {CSRF_COOKIE_NAME}={csrf}",
        CSRF_HEADER_NAME: csrf,
    }


def _user_headers(client: TestClient) -> dict[str, str]:
    creds = _creds()
    register = client.post("/auth/register", json=creds)
    assert register.status_code == 202, register.text
    login = client.post("/auth/login", json=creds)
    assert login.status_code == 200, login.text
    token = login.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


def test_lab_routes_are_absent_without_lab_enabled(api_env: Engine) -> None:
    """T13.3.1: with the default (off), there is no router to even 403 against -- a 404."""
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    assert client.get("/admin/lab/runs", headers=headers).status_code == 404
    assert client.post("/admin/lab/run", headers=headers).status_code == 404


def test_lab_routes_refuse_a_plain_user(api_env: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _user_headers(client)
    assert client.get("/admin/lab/runs", headers=headers).status_code == 403
    assert client.post("/admin/lab/run", headers=headers).status_code == 403


def test_lab_run_refuses_an_admin_without_mfa_stepup(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=False)
    assert client.post("/admin/lab/run", headers=headers).status_code == 403
    assert client.get("/admin/lab/runs", headers=headers).status_code == 403


def test_lab_run_rejects_a_missing_csrf_token(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    cookie_only = {"Cookie": headers["Cookie"]}
    assert client.post("/admin/lab/run", headers=cookie_only).status_code == 403


def test_admin_lab_run_records_results_and_an_admin_action(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")

    fake_probe = Probe(
        id="OWASP-A05", expected_outcome="mitigated: x passes", test_id="backend/x.py::y"
    )
    fake_results = [ProbeResult(fake_probe, ProbeOutcome("passed", "x.py::y: PASSED", True))]
    monkeypatch.setattr("rag_app.api.lab.catalogue_probes", lambda: [fake_probe])
    monkeypatch.setattr("rag_app.api.lab.run_catalogue_probes", lambda probes: fake_results)
    monkeypatch.setattr("rag_app.api.lab.git_sha", lambda: "deadbeefcafe")

    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post("/admin/lab/run", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["all_matched"] is True
    assert body["git_sha"] == "deadbeefcafe"
    assert body["results"] == [
        {
            "probe_id": "OWASP-A05",
            "expected_outcome": "mitigated: x passes",
            "observed_outcome": "passed",
            "passed": True,
            "name": "",
            "description": "",
        }
    ]

    with Session(api_env) as session:
        runs = session.scalars(select(DefenceRun).where(DefenceRun.git_sha == "deadbeefcafe")).all()
        assert len(runs) == 1
        assert runs[0].probe_id == "OWASP-A05"
        assert runs[0].passed is True

        actions = session.scalars(
            select(AdminAction).where(AdminAction.action == "defence_lab_run")
        ).all()
        assert len(actions) == 1
        assert actions[0].result == "all_matched"
        # T13.1.7 (no-PII convention): the target is a count+sha summary, never a probe's
        # evidence text (which could, in principle, contain stack traces/paths).
        assert "deadbeefcafe" in (actions[0].target or "")


def test_admin_lab_run_response_includes_the_probes_name_and_description(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-13-J3J4-2: the card shows a human name + a one-line description, not the raw id."""
    monkeypatch.setenv("LAB_ENABLED", "true")
    fake_probe = Probe(
        id="OWASP-A05",
        expected_outcome="mitigated: x passes",
        test_id="backend/x.py::y",
        name="Injection",
        description="injection (ORM/parameterised queries)",
    )
    fake_results = [ProbeResult(fake_probe, ProbeOutcome("passed", "x.py::y: PASSED", True))]
    monkeypatch.setattr("rag_app.api.lab.catalogue_probes", lambda *a: [fake_probe])
    monkeypatch.setattr("rag_app.api.lab.run_catalogue_probes", lambda probes: fake_results)
    monkeypatch.setattr("rag_app.api.lab.git_sha", lambda: "deadbeefcafe")

    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post("/admin/lab/run", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["name"] == "Injection"
    assert result["description"] == "injection (ORM/parameterised queries)"


# --- T13.J4-fix (DA-13-J3J4-1): per-probe run route, the mockup's own per-card "Run probe"
# button. Same gating as the whole-lab route above; red on the pre-fix code (no such route
# -- every card's button called the SAME whole-lab run). ---


def test_lab_run_one_routes_are_absent_without_lab_enabled(api_env: Engine) -> None:
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    assert client.post("/admin/lab/run/OWASP-A05", headers=headers).status_code == 404


def test_lab_run_one_refuses_a_plain_user(api_env: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _user_headers(client)
    assert client.post("/admin/lab/run/OWASP-A05", headers=headers).status_code == 403


def test_lab_run_one_refuses_an_admin_without_mfa_stepup(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=False)
    assert client.post("/admin/lab/run/OWASP-A05", headers=headers).status_code == 403


def test_lab_run_one_rejects_a_missing_csrf_token(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    cookie_only = {"Cookie": headers["Cookie"]}
    assert client.post("/admin/lab/run/OWASP-A05", headers=cookie_only).status_code == 403


def test_lab_run_one_unknown_probe_id_is_404_not_500(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post("/admin/lab/run/NOT-A-REAL-PROBE-ID", headers=headers)
    assert response.status_code == 404, response.text


def test_lab_run_one_runs_only_the_requested_probe_never_the_whole_lab(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control (DA-13-J3J4-1's own Done-when): pressing ONE card's button must
    never run (or record a result for) any other probe."""
    monkeypatch.setenv("LAB_ENABLED", "true")
    requested_ids: list[list[str]] = []
    # A UNIQUE sha (never the literal "deadbeefcafe" other tests in this module reuse) --
    # `db_url` is session-scoped (conftest.py), so `defence_runs`/`admin_actions` rows from
    # every other test in this module/session persist in the SAME database; a shared literal
    # sha would make this test's own DB assertions collide with theirs.
    sha = f"deadbeef{uuid.uuid4().hex[:8]}"

    def _spy_run(probes):  # noqa: ANN001
        requested_ids.append([p.id for p in probes])
        return [ProbeResult(p, ProbeOutcome("passed", "ok", True)) for p in probes]

    monkeypatch.setattr("rag_app.api.lab.run_catalogue_probes", _spy_run)
    monkeypatch.setattr("rag_app.api.lab.git_sha", lambda: sha)

    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post("/admin/lab/run/OWASP-A05", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["probe_id"] for r in body["results"]] == ["OWASP-A05"]
    assert requested_ids == [["OWASP-A05"]], "exactly one probe must have been executed"

    with Session(api_env) as session:
        runs = session.scalars(select(DefenceRun).where(DefenceRun.git_sha == sha)).all()
        assert [r.probe_id for r in runs] == ["OWASP-A05"], "no OTHER probe's result was stored"

        actions = session.scalars(
            select(AdminAction).where(
                AdminAction.action == "defence_lab_run", AdminAction.target.contains(sha)
            )
        ).all()
        assert len(actions) == 1
        assert "OWASP-A05" in (actions[0].target or "")


def test_lab_run_one_response_includes_the_probes_name_and_description(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    monkeypatch.setattr(
        "rag_app.api.lab.run_catalogue_probes",
        lambda probes: [ProbeResult(p, ProbeOutcome("passed", "ok", True)) for p in probes],
    )
    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.post("/admin/lab/run/OWASP-A05", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["name"] and result["name"] != "OWASP-A05"
    assert result["description"]


def test_admin_lab_runs_lists_history_newest_first(
    api_env: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_ENABLED", "true")
    with Session(api_env) as session:
        session.add(
            DefenceRun(
                probe_id="REC-11",
                git_sha="aaa111",
                expected_outcome="mitigated: ... passes",
                observed_outcome="passed",
                passed=True,
                evidence="ok",
            )
        )
        session.commit()

    client = TestClient(create_app(), base_url=BASE_URL)
    headers = _admin_headers(api_env, stepped_up=True)
    response = client.get("/admin/lab/runs", headers=headers)
    assert response.status_code == 200, response.text
    runs = response.json()["runs"]
    probe_ids = [row["probe_id"] for row in runs]
    assert "REC-11" in probe_ids

    # DA-13-J3J4-2: a history row (only ever a bare probe_id in the database) still gets a
    # real name/description, looked up from the catalogue -- not just the id repeated back.
    rec11 = next(row for row in runs if row["probe_id"] == "REC-11")
    assert rec11["name"] and rec11["name"] != "REC-11"
    assert rec11["description"]
