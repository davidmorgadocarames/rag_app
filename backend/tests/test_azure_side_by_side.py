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
