"""Offline, mocked tests for ``rag_app.eval.azure_side_by_side`` (14.7-migration-live, part 2b).

No network call, no ``~/.config/secrag`` reference anywhere in this file (F43) -- every Azure
response is a fake object injected via ``monkeypatch``/a ``post=`` kwarg. Covers the properties
the live-check build is actually graded on: the cost guard genuinely stops a call before the cap
(positive + negative control), the output never contains a planted fake secret (leak test), the
API-version probe picks the first working version, and the shell script never echoes the API key
and keeps its executable bit.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from rag_app.eval import azure_side_by_side as sbs
from rag_app.eval.dataset import Dialogue, Turn
from rag_app.eval.metrics import DialogueTurnResult

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "azure" / "openai_live_check.sh"


def _resolve_openai_live_check_venv_python() -> Path:
    """Mirrors ``openai_live_check.sh``'s OWN venv resolution (git-common-dir -> MAIN_ROOT/
    backend/.venv, falling back to REPO_ROOT/backend/.venv -- no ``GATE_VENV`` override, unlike
    ``scripts/dev/verify.sh``) so the skip check below asks exactly the same question the script
    itself asks -- never a looser/different one that could pass here and still fail inside the
    subprocess. A CI job that runs backend pytest straight against the system interpreter (no
    ``backend/.venv`` at all, e.g. this repo's "Backend (lint, types, tests incl. DB)" job) hits
    that BEFORE any test assertion runs, which would otherwise surface as a confusing
    non-zero-exit failure instead of a clean skip (same pattern as
    ``test_feature_map_verify.py``'s own skip for ``scripts/dev/verify.sh``)."""
    common_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    main_root = Path(common_dir).parent if common_dir else REPO_ROOT
    for candidate_root in (main_root, REPO_ROOT):
        candidate = candidate_root / "backend" / ".venv" / "bin" / "python"
        if os.access(candidate, os.X_OK):
            return candidate
    return main_root / "backend" / ".venv" / "bin" / "python"


_OPENAI_LIVE_CHECK_VENV_PYTHON = _resolve_openai_live_check_venv_python()

FAKE_PRICES: dict[str, Any] = {
    "fx_usd_to_eur": 0.92,
    "openai": {
        "models": {
            "gpt-4.1-mini": {
                "datazone_standard": {"input_per_1m_usd": 0.44, "output_per_1m_usd": 1.76},
            },
            "gpt-5-mini": {
                "datazone_standard": {"input_per_1m_usd": 0.275, "output_per_1m_usd": 2.20},
            },
        }
    },
}


def _guard(max_eur: float) -> sbs.CostGuard:
    return sbs.CostGuard(prices=FAKE_PRICES, max_eur=max_eur)


# --- CostGuard: positive + negative control ------------------------------------------------


def test_cost_guard_positive_control_commits_within_budget() -> None:
    guard = _guard(max_eur=1.0)
    entry = guard.reserve("phase-x", model="gpt-4.1-mini", tokens_in=1000.0, tokens_out=200.0)
    assert entry["eur"] > 0.0
    assert guard.spent_eur == pytest.approx(entry["eur"])
    assert guard.log == [entry]


def test_cost_guard_negative_control_blocks_before_the_cap_and_commits_nothing() -> None:
    guard = _guard(max_eur=0.0001)  # a cap far below even a single reservation
    with pytest.raises(sbs.CostCapExceeded):
        guard.reserve("phase-x", model="gpt-5-mini", tokens_in=1_000_000.0, tokens_out=200_000.0)
    assert guard.spent_eur == 0.0  # no partial charge
    assert guard.log == []  # nothing committed


def test_cost_guard_negative_control_prevents_the_call_itself() -> None:
    """The guard must be checked BEFORE the call -- this proves ``run_smoke`` never reaches
    ``httpx.post`` once the cap is already exhausted."""
    calls: list[str] = []

    def fake_post(url: str, **_kw: Any) -> httpx.Response:
        calls.append(url)
        raise AssertionError("must never be called once the cost cap is exhausted")

    guard = _guard(max_eur=0.0)  # exhausted before the very first reservation
    settings = _fake_settings()
    with pytest.raises(sbs.CostCapExceeded):
        sbs.run_smoke(settings, guard)
    assert calls == []


def test_cost_guard_running_total_accumulates_across_reservations() -> None:
    guard = _guard(max_eur=1.0)
    first = guard.reserve("a", model="gpt-4.1-mini", tokens_in=1000.0, tokens_out=100.0)
    second = guard.reserve("b", model="gpt-4.1-mini", tokens_in=1000.0, tokens_out=100.0)
    assert guard.spent_eur == pytest.approx(first["eur"] + second["eur"])
    assert second["running_total_eur"] == pytest.approx(guard.spent_eur)


def test_cost_guard_from_file_reads_prices_json(tmp_path: Path) -> None:
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    guard = sbs.CostGuard.from_file(1.0, path=prices_path)
    assert guard.max_eur == 1.0
    assert guard.prices == FAKE_PRICES


def test_cost_guard_from_file_raises_cleanly_when_path_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sbs.CostGuard.from_file(1.0, path=tmp_path / "does-not-exist.json")


# --- resolve_prices_path: resolution order (part 2c, F22-class worktree bug) ----------------
#
# The bug this part fixes: a worktree's own status/ (e.g. ~/proyectos/wt/14-A1/status/) has no
# status symlink and so holds only status/blocks/, never azure_prices.json -- the old
# DEFAULT_PRICES_PATH pointed unconditionally at REPO_ROOT/status/azure_prices.json, which is
# exactly this missing path from a worktree.


def test_resolve_prices_path_prefers_explicit_over_every_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}", encoding="utf-8")
    env_path = tmp_path / "env.json"
    env_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(sbs.SECRAG_AZURE_PRICES_ENV, str(env_path))
    assert sbs.resolve_prices_path(explicit) == explicit


def test_resolve_prices_path_falls_back_to_env_var_when_no_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / "env.json"
    env_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(sbs.SECRAG_AZURE_PRICES_ENV, str(env_path))
    monkeypatch.setattr(sbs, "REPO_ROOT", tmp_path / "no-such-repo")
    monkeypatch.setattr(sbs, "MAIN_REPO_PRICES_PATH", tmp_path / "no-such-main-repo.json")
    assert sbs.resolve_prices_path(None) == env_path


def test_resolve_prices_path_falls_back_to_repo_root_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sbs.SECRAG_AZURE_PRICES_ENV, raising=False)
    repo_root = tmp_path / "repo"
    (repo_root / "status").mkdir(parents=True)
    repo_prices = repo_root / "status" / "azure_prices.json"
    repo_prices.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sbs, "REPO_ROOT", repo_root)
    monkeypatch.setattr(sbs, "MAIN_REPO_PRICES_PATH", tmp_path / "no-such-main-repo.json")
    assert sbs.resolve_prices_path(None) == repo_prices


def test_resolve_prices_path_falls_back_to_main_repo_when_worktree_status_is_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact worktree bug this part fixes: REPO_ROOT/status/ exists (e.g. only
    status/blocks/, like a real worktree) but has no azure_prices.json -- must fall through to
    the main-repo fallback, not raise."""
    monkeypatch.delenv(sbs.SECRAG_AZURE_PRICES_ENV, raising=False)
    worktree_root = tmp_path / "worktree"
    (worktree_root / "status" / "blocks").mkdir(parents=True)  # no azure_prices.json here
    monkeypatch.setattr(sbs, "REPO_ROOT", worktree_root)
    main_repo_prices = tmp_path / "main-repo-status" / "azure_prices.json"
    main_repo_prices.parent.mkdir(parents=True)
    main_repo_prices.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sbs, "MAIN_REPO_PRICES_PATH", main_repo_prices)
    assert sbs.resolve_prices_path(None) == main_repo_prices


def test_resolve_prices_path_fails_closed_naming_every_path_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_explicit = tmp_path / "missing-explicit.json"
    missing_env = tmp_path / "missing-env.json"
    missing_repo_root = tmp_path / "missing-repo"
    missing_main_repo = tmp_path / "missing-main-repo.json"
    monkeypatch.setenv(sbs.SECRAG_AZURE_PRICES_ENV, str(missing_env))
    monkeypatch.setattr(sbs, "REPO_ROOT", missing_repo_root)
    monkeypatch.setattr(sbs, "MAIN_REPO_PRICES_PATH", missing_main_repo)

    with pytest.raises(FileNotFoundError) as excinfo:
        sbs.resolve_prices_path(missing_explicit)

    message = str(excinfo.value)
    for expected in (
        str(missing_explicit),
        str(missing_env),
        str(missing_repo_root / "status" / "azure_prices.json"),
        str(missing_main_repo),
    ):
        assert expected in message


# --- check_config_lines() / --check-config: no env file, no network ------------------------


def test_check_config_lines_reports_resolved_path_and_datazone_prices(tmp_path: Path) -> None:
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    lines = sbs.check_config_lines(prices_path)
    joined = "\n".join(lines)
    assert f"prices file: {prices_path}" in joined
    assert "gpt-4.1-mini (datazone_standard): input=0.44" in joined
    assert "gpt-5-mini (datazone_standard): input=0.275" in joined
    assert "golden set v2:" in joined


def test_check_config_lines_fails_closed_when_nothing_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sbs.SECRAG_AZURE_PRICES_ENV, raising=False)
    monkeypatch.setattr(sbs, "REPO_ROOT", tmp_path / "no-repo")
    monkeypatch.setattr(sbs, "MAIN_REPO_PRICES_PATH", tmp_path / "no-main-repo.json")
    with pytest.raises(FileNotFoundError):
        sbs.check_config_lines(None)


def test_check_config_lines_never_calls_get_settings_or_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("check_config_lines must never call get_settings or the network")

    monkeypatch.setattr(sbs, "get_settings", _boom)
    monkeypatch.setattr(sbs.httpx, "post", _boom)
    sbs.check_config_lines(prices_path)  # must not raise


def test_main_check_config_flag_prints_lines_and_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("--check-config must never touch settings or the network")

    monkeypatch.setattr(sbs, "get_settings", _boom)
    monkeypatch.setattr(sbs.httpx, "post", _boom)

    exit_code = sbs.main(["--check-config", "--prices", str(prices_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"prices file: {prices_path}" in captured.out
    assert "gpt-5-mini" in captured.out


def test_main_without_a_resolvable_prices_file_fails_closed_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(sbs.SECRAG_AZURE_PRICES_ENV, raising=False)
    monkeypatch.setattr(sbs, "REPO_ROOT", tmp_path / "no-repo")
    monkeypatch.setattr(sbs, "MAIN_REPO_PRICES_PATH", tmp_path / "no-main-repo.json")

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("main() must fail closed before calling get_settings or the network")

    monkeypatch.setattr(sbs, "get_settings", _boom)
    monkeypatch.setattr(sbs.httpx, "post", _boom)

    exit_code = sbs.main([])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "no Azure OpenAI prices file found" in captured.err


@pytest.mark.skipif(
    not os.access(_OPENAI_LIVE_CHECK_VENV_PYTHON, os.X_OK),
    reason="needs a resolvable backend venv (scripts/azure/openai_live_check.sh's own"
    " prerequisite -- see _resolve_openai_live_check_venv_python())",
)
def test_openai_live_check_script_check_config_works_without_the_secret_env_file() -> None:
    """--check-config must succeed even though the real $HOME/.config/secrag/azure-openai.env
    on this machine is never sourced, read or checked for existence on this path -- proves the
    secret-env-file gate is genuinely skipped for --check-config, not just documented as
    skipped. Uses the real $HOME (not a fake one) specifically so the real
    ~/proyectos/rag_app/status/azure_prices.json fallback is reachable, exactly like a real
    worktree run."""
    env = dict(os.environ)
    env.pop(sbs.SECRAG_AZURE_PRICES_ENV, None)
    result = subprocess.run(
        ["bash", str(SCRIPT_PATH), "--check-config"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "prices file:" in result.stdout
    assert "SECRET_ENV_FILE" not in result.stdout + result.stderr


# --- API-version probe ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_probe_api_versions_picks_the_first_working_version() -> None:
    attempted: list[str] = []

    def fake_post(url: str, **_kw: Any) -> _FakeResponse:
        version = url.split("api-version=")[-1]
        attempted.append(version)
        # the SECOND candidate in GA_API_VERSION_PROBE_ORDER is the one that "works" here
        if version == sbs.GA_API_VERSION_PROBE_ORDER[1]:
            return _FakeResponse(200)
        return _FakeResponse(400)

    version, attempts = sbs.probe_api_versions(
        endpoint="http://fake.example",
        api_key="unused",
        deployment="gpt-5-mini",
        post=fake_post,
    )
    assert version == sbs.GA_API_VERSION_PROBE_ORDER[1]
    assert attempted == list(sbs.GA_API_VERSION_PROBE_ORDER[:2])  # stopped probing after success
    assert attempts[0] == {
        "api_version": sbs.GA_API_VERSION_PROBE_ORDER[0],
        "status_code": 400,
        "error_type": "HTTPStatusError",
    }
    assert attempts[1]["status_code"] == 200
    assert attempts[1]["error_type"] is None


def test_probe_api_versions_raises_when_every_candidate_fails() -> None:
    def fake_post(_url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(400)

    with pytest.raises(RuntimeError, match="no candidate API version succeeded"):
        sbs.probe_api_versions(
            endpoint="http://fake.example",
            api_key="unused",
            deployment="gpt-5-mini",
            post=fake_post,
        )


def test_probe_api_versions_never_includes_a_response_body() -> None:
    """Only api_version/status_code/error_type -- never a 'body'/'content'/'message' key that
    could leak request/response text."""

    def fake_post(_url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(400)

    with pytest.raises(RuntimeError):
        sbs.probe_api_versions(
            endpoint="http://fake.example",
            api_key="unused",
            deployment="gpt-5-mini",
            post=fake_post,
        )

    # also check the success path's attempt dict shape
    def fake_post_ok(_url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(200)

    _version, attempts = sbs.probe_api_versions(
        endpoint="http://fake.example", api_key="unused", deployment="gpt-5-mini", post=fake_post_ok
    )
    for attempt in attempts:
        assert set(attempt) == {"api_version", "status_code", "error_type"}


# --- Leak test: a planted fake endpoint/key never reach the output -------------------------


_FAKE_ENDPOINT = "https://FAKE-ENDPOINT-f3a9c1.openai.azure.com"
_FAKE_KEY = "FAKE-SECRET-KEY-9f8e7d6c5b4a"


def _fake_settings(**overrides: Any) -> Any:
    from rag_app.config import Settings

    base: dict[str, Any] = {
        "llm_provider": "azure_openai",
        "azure_openai_endpoint": _FAKE_ENDPOINT,
        "azure_openai_api_key": _FAKE_KEY,
        "azure_openai_deployment": "gpt-4.1-mini",
    }
    base.update(overrides)
    return Settings(**base)


def _assert_no_leak(*blobs: str) -> None:
    for blob in blobs:
        assert _FAKE_ENDPOINT not in blob
        assert _FAKE_KEY not in blob


def test_run_smoke_output_never_contains_the_fake_endpoint_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], **_kw: Any
    ) -> httpx.Response:
        # sanity: the fake secret really is being sent on the wire (proves this is a real test,
        # not a vacuous pass) -- it just must never come back OUT in the report.
        assert headers["api-key"] == _FAKE_KEY
        assert _FAKE_ENDPOINT in url
        request = httpx.Request("POST", url)
        return httpx.Response(400, request=request, json={"error": {"message": "bad request"}})

    monkeypatch.setattr(sbs.httpx, "post", fake_post)
    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    result = sbs.run_smoke(settings, guard)

    blob = json.dumps(result)
    _assert_no_leak(blob)
    _assert_no_leak(sbs.summarize({"smoke": result, "total_eur": guard.spent_eur}))
    # the CostCapExceeded message path is exercised separately but must be leak-free too
    with pytest.raises(sbs.CostCapExceeded) as excinfo:
        _guard(max_eur=0.0).reserve("smoke", model="gpt-4.1-mini", tokens_in=1.0, tokens_out=1.0)
    _assert_no_leak(str(excinfo.value))


def test_cost_cap_exceeded_message_never_contains_an_endpoint_field() -> None:
    guard = _guard(max_eur=0.0)
    with pytest.raises(sbs.CostCapExceeded) as excinfo:
        guard.reserve("side-by-side:gpt-5-mini", model="gpt-5-mini", tokens_in=1.0, tokens_out=1.0)
    message = str(excinfo.value)
    assert "http" not in message.lower()
    assert "endpoint" not in message.lower()


# --- Smoke phase: reasoning-model 400 triggers the api-version probe -----------------------


def test_run_smoke_probes_api_versions_on_a_400_for_the_reasoning_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        is_json_call = "response_format" in json
        if is_json_call:
            # chat_json is not under test here -- always succeed so chat_json_ok stays True.
            return httpx.Response(
                200, request=request, json={"choices": [{"message": {"content": '{"ok": true}'}}]}
            )
        if "gpt-5-mini" in url:
            # Both run_smoke's own .chat() call (settings.azure_openai_reasoning_api_version,
            # the probe order's FIRST candidate) and probe_api_versions' own retry of that same
            # first candidate 400; the probe's SECOND candidate succeeds.
            version = url.split("api-version=")[-1]
            if version == sbs.GA_API_VERSION_PROBE_ORDER[1]:
                return httpx.Response(
                    200, request=request, json={"choices": [{"message": {"content": "ok"}}]}
                )
            return httpx.Response(400, request=request, json={"error": {"message": "bad request"}})
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr(sbs.httpx, "post", fake_post)

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    result = sbs.run_smoke(settings, guard)

    candidate_entry = result[sbs.CANDIDATE_DEPLOYMENT]
    assert candidate_entry["chat_ok"] is False
    assert candidate_entry["chat_status_code"] == 400
    assert candidate_entry["api_version_recommended"] == sbs.GA_API_VERSION_PROBE_ORDER[1]


# --- Token budget phase: measurements never carry prompt/response text ---------------------


def _fixture_dialogues() -> list[Dialogue]:
    turn = Turn(
        message="What is SQL injection?",
        expected_intent="security",
        language="en",
        expected_doc="cs-sql-injection-prevention",
        answerable=True,
        ground_truth="SQL injection is an attack where untrusted input is inserted into a query.",
    )
    return [Dialogue(id="fixture-1", turns=(turn,))]


def test_representative_prompts_builds_router_answer_groundedness_triples() -> None:
    prompts = sbs.representative_prompts(_fixture_dialogues(), n=3)
    call_types = [call_type for call_type, _ in prompts]
    assert call_types == ["router", "answer", "groundedness"]


def test_measure_token_budget_never_stores_prompt_or_response_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(_url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        request = httpx.Request("POST", "http://fake")
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "the answer text, never stored"}}],
                "usage": {
                    "completion_tokens": 120,
                    "completion_tokens_details": {"reasoning_tokens": 80},
                },
            },
        )

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    result = sbs.measure_token_budget(settings, guard, _fixture_dialogues(), post=fake_post)

    for measurement in result["measurements"]:
        assert set(measurement) == {
            "call_type",
            "bound",
            "completion_tokens",
            "reasoning_tokens",
            "visible_tokens",
        }
        assert measurement["reasoning_tokens"] == 80
        assert measurement["visible_tokens"] == 40
    assert "the answer text" not in json.dumps(result)
    assert result["recommended_max_tokens"] is not None


# --- Side-by-side phase: cost guard still gates it, metrics flow through -------------------


def test_run_side_by_side_reserves_cost_before_evaluating_and_reports_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_result = DialogueTurnResult(
        dialogue_id="fixture-1",
        turn_index=0,
        expected_intent="security",
        observed_intent="security",
        is_noisy=False,
        retrieved_docs=["cs-sql-injection-prevention"],
        expected_doc="cs-sql-injection-prevention",
        answerable=True,
        abstained=False,
        reply_text="Use parameterized queries [1].",
        correct=True,
        clarified=False,
        abstention_no_claims=None,
        latency_s=0.5,
    )

    monkeypatch.setattr(
        sbs,
        "evaluate_dialogues",
        lambda session, dialogues, chat=None, top_n=None: [fixture_result],
    )

    class _FakeSession:
        def __enter__(self) -> _FakeSession:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    subset = sbs.azure_subset(_fixture_dialogues(), size=30)
    result = sbs.run_side_by_side(settings, guard, lambda: _FakeSession(), subset)

    assert set(result) == {sbs.BASELINE_DEPLOYMENT, sbs.CANDIDATE_DEPLOYMENT}
    for entry in result.values():
        assert entry["n_turns"] == 1
        assert entry["metrics"]["intent_accuracy"] == 1.0
    assert len(guard.log) == 2  # one reservation per deployment


def test_azure_subset_is_the_first_n_dialogues_deterministically() -> None:
    dialogues = [
        Dialogue(id=f"d{i}", turns=(Turn(message="hi", expected_intent="chitchat", language="en"),))
        for i in range(40)
    ]
    subset = sbs.azure_subset(dialogues)
    assert len(subset) == sbs.AZURE_SUBSET_SIZE == 30
    assert [d.id for d in subset] == [f"d{i}" for i in range(30)]


# --- Summary format --------------------------------------------------------------------------


def test_summarize_includes_total_eur_and_per_deployment_status() -> None:
    report = {
        "total_eur": 0.1234,
        "smoke": {
            "gpt-4.1-mini": {"chat_ok": True, "chat_json_ok": True},
            "gpt-5-mini": {
                "chat_ok": False,
                "chat_error_type": "HTTPStatusError",
                "chat_json_ok": True,
                "api_version_recommended": "2024-06-01",
            },
        },
    }
    text = sbs.summarize(report)
    assert "0.1234" in text
    assert "gpt-4.1-mini: chat=ok chat_json=ok" in text
    assert "gpt-5-mini: chat=FAIL (HTTPStatusError) chat_json=ok" in text
    assert "recommended api-version: 2024-06-01" in text


def test_summarize_reports_a_cost_guard_block(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "total_eur": 0.0,
        "smoke_blocked": "smoke: projected EUR 5.0 would exceed --max-eur 0.3",
    }
    text = sbs.summarize(report)
    assert "COST GUARD STOPPED smoke_blocked" in text


# --- The shell script: shellcheck-clean, syntax-clean, never echoes the key, keeps +x ------


def test_openai_live_check_script_exists_and_is_executable() -> None:
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/azure/openai_live_check.sh must keep its executable bit"


def test_openai_live_check_script_bash_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_openai_live_check_script_never_echoes_the_secret_variables() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    secret_names = ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for name in secret_names:
            if name in stripped:
                # the only sanctioned mentions: the module docstring comment block (already
                # skipped above) and the `set -a ... ; . "$SECRET_ENV_FILE" ; set +a` sourcing
                # line, which never names the variables directly.
                assert (
                    "echo" not in stripped and "print" not in stripped
                ), f"line echoes/prints a secret variable name: {line!r}"


def test_openai_live_check_script_sources_via_set_a_set_plus_a() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "set -a" in text
    assert "set +a" in text
    assert "SECRET_ENV_FILE" in text


def test_openai_live_check_script_disables_xtrace() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "set +o xtrace" in text or "set +x" in text


@pytest.mark.skipif(
    not os.environ.get("SECRAG_RUN_SHELLCHECK_TEST"),
    reason="shellcheck run by scripts/gate.sh's own step",
)
def test_openai_live_check_script_is_shellcheck_clean() -> None:
    result = subprocess.run(["shellcheck", str(SCRIPT_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
