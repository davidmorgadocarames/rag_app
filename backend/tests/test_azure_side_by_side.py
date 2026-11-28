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


# --- CostGuard.settle(): true-up to real usage (DA-14.7-migration-live-2a-1, part 2d) -------


def test_cost_guard_settle_reduces_spent_eur_when_real_usage_is_lower() -> None:
    guard = _guard(max_eur=1.0)
    entry = guard.reserve("phase", model="gpt-5-mini", tokens_in=200.0, tokens_out=3072.0)
    reserved_eur = entry["eur"]
    settled = guard.settle(entry, tokens_in=200.0, tokens_out=50.0)
    assert guard.spent_eur < reserved_eur
    assert guard.spent_eur == pytest.approx(settled["actual_eur"])
    assert entry["actual_tokens_out"] == 50.0


def test_cost_guard_settle_a_fake_gpt5_mini_large_reasoning_token_count_trues_up_not_blocks() -> (
    None
):
    """A fake gpt-5-mini response reporting a LARGE ``completion_tokens`` (reasoning tokens
    counted as part of the output total, same as Azure's own usage field) settles the cost guard
    UP to the real usage -- never raises, even past the worst-case bound that was reserved: the
    call already happened and cannot be undone."""
    guard = _guard(max_eur=1.0)
    entry = guard.reserve(
        "token-budget:answer", model="gpt-5-mini", tokens_in=200.0, tokens_out=100.0
    )
    settled = guard.settle(entry, tokens_in=200.0, tokens_out=50_000.0)
    assert settled["actual_tokens_out"] == 50_000.0
    assert guard.spent_eur == pytest.approx(settled["actual_eur"])
    assert guard.spent_eur > entry["eur"]  # trued UP well past the worst-case reservation


# --- make_guarded_post: per-CALL reservation + settlement (DA-14.7-migration-live-2a-1) -----


_FAKE_GPT5_URL = (
    "https://fake.example.openai.azure.com/openai/deployments/gpt-5-mini/"
    "chat/completions?api-version=2024-10-21"
)


def test_make_guarded_post_reserves_the_real_per_call_bound_not_a_flat_estimate() -> None:
    def fake_post(url: str, *, headers: Any, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"completion_tokens": 10, "prompt_tokens": 20},
            },
        )

    guard = _guard(max_eur=1.0)
    guarded = sbs.make_guarded_post(guard, "test-phase", post=fake_post)
    guarded(
        _FAKE_GPT5_URL,
        headers={},
        json={"messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 3072},
        timeout=30,
    )
    assert guard.log[0]["model"] == "gpt-5-mini"
    assert guard.log[0]["tokens_out"] == 3072.0  # the real worst case reserved, not a flat 600
    assert guard.log[0]["actual_tokens_out"] == 10.0  # settled down to the real usage
    assert guard.spent_eur == pytest.approx(guard.log[0]["actual_eur"])


def test_make_guarded_post_refuses_a_call_with_no_max_tokens_bound() -> None:
    calls: list[int] = []

    def fake_post(*_a: Any, **_kw: Any) -> httpx.Response:
        calls.append(1)
        raise AssertionError("must never be called without a reservable bound")

    guard = _guard(max_eur=1.0)
    guarded = sbs.make_guarded_post(guard, "test-phase", post=fake_post)
    with pytest.raises(RuntimeError, match="no worst case to reserve"):
        guarded(_FAKE_GPT5_URL, headers={}, json={"messages": []}, timeout=30)
    assert calls == []
    assert guard.log == []


def test_make_guarded_post_negative_control_blocks_before_the_real_call() -> None:
    calls: list[int] = []

    def fake_post(*_a: Any, **_kw: Any) -> httpx.Response:
        calls.append(1)
        raise AssertionError("must never be called once the cap is exhausted")

    guard = _guard(max_eur=0.0)
    guarded = sbs.make_guarded_post(guard, "test-phase", post=fake_post)
    with pytest.raises(sbs.CostCapExceeded):
        guarded(
            _FAKE_GPT5_URL,
            headers={},
            json={"messages": [], "max_completion_tokens": 100},
            timeout=30,
        )
    assert calls == []


def test_make_guarded_post_429_retry_re_reserves_without_double_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sbs.time, "sleep", lambda *_a, **_kw: None)
    attempts: list[int] = []

    def fake_post(url: str, *, headers: Any, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, request=request, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"completion_tokens": 10, "prompt_tokens": 20},
            },
        )

    guard = _guard(max_eur=1.0)
    guarded = sbs.make_guarded_post(guard, "test-phase", post=fake_post)
    response = guarded(
        _FAKE_GPT5_URL,
        headers={},
        json={"messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 3072},
        timeout=30,
    )
    assert response.status_code == 200
    assert len(attempts) == 2
    assert len(guard.log) == 2  # one log entry per attempt, kept for audit
    assert guard.log[0]["actual_eur"] == 0.0  # the rejected 429 attempt billed nothing
    # the running total reflects ONLY the successful retry -- never double-counted.
    assert guard.spent_eur == pytest.approx(guard.log[1]["actual_eur"])


def test_make_guarded_post_stops_before_call_n_when_the_cap_is_tight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_post(url: str, *, headers: Any, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls.append(1)
        request = httpx.Request("POST", url)
        # No "usage" key -- settle() then falls back to the full worst-case bound, so each
        # successful call's TRUED-UP cost still equals its own reservation (makes the running
        # total grow predictably, call after call, for this test's cap-sizing arithmetic).
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": "ok"}}]}
        )

    body = {"messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 3072}
    # tokens_in exactly matches `_estimate_input_tokens` on this body's own messages (len("hi")
    # / 4 == 0.5) so the cap below lines up exactly with what `_guarded` will actually reserve.
    one_call_eur = _guard(max_eur=1.0).estimate(
        model="gpt-5-mini", tokens_in=0.5, tokens_out=3072.0
    )["eur"]
    # cap big enough for exactly 2 calls' worst case, too tight for a 3rd.
    guard = _guard(max_eur=one_call_eur * 2.0 + one_call_eur * 0.5)
    guarded = sbs.make_guarded_post(guard, "test-phase", post=fake_post)

    guarded(_FAKE_GPT5_URL, headers={}, json=body, timeout=30)
    guarded(_FAKE_GPT5_URL, headers={}, json=body, timeout=30)
    with pytest.raises(sbs.CostCapExceeded):
        guarded(_FAKE_GPT5_URL, headers={}, json=body, timeout=30)
    assert len(calls) == 2  # stopped BEFORE the 3rd call, never made it


def test_deployment_from_url_returns_none_for_a_non_azure_url() -> None:
    """DA-14.7-migration-live-2e-1: Ollama's own /api/embed (or any other non-Azure-chat
    endpoint) must never raise -- just signal "not my concern" to the caller."""
    assert sbs._deployment_from_url("http://localhost:11434/api/embed") is None
    assert sbs._deployment_from_url(_FAKE_GPT5_URL) == "gpt-5-mini"


def test_make_guarded_post_passes_a_non_azure_call_straight_through_unreserved() -> None:
    """DA-14.7-migration-live-2e-1 (THE root-cause fix): the FIRST real user run's exact crash
    -- the globally-patched ``httpx.post`` (``_patched_post``, used by phase (c)) also intercepts
    an Ollama embeddings call made during real retrieval; the old ``_deployment_from_url`` raised
    a bare ``RuntimeError`` ("could not identify the Azure deployment"), unhandled anywhere, which
    lost every phase's already-computed results. Now it must pass straight through: real_post is
    called, nothing is reserved, no exception."""
    calls: list[str] = []

    def fake_post(url: str, **_kw: Any) -> httpx.Response:
        calls.append(url)
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"embeddings": [[0.1, 0.2]]})

    guard = _guard(max_eur=1.0)
    guarded = sbs.make_guarded_post(guard, "side-by-side:test", post=fake_post)
    response = guarded(
        "http://localhost:11434/api/embed",
        json={"model": "bge-m3", "input": ["hello"]},
        timeout=170,
    )
    assert response.status_code == 200
    assert calls == ["http://localhost:11434/api/embed"]  # real_post genuinely reached
    assert guard.log == []  # nothing reserved -- not this guard's concern


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


# --- Smoke phase: reasoning-budget exhaustion (part 2e, the FIRST real user run's exact bug) -


def test_run_smoke_uses_a_generous_bound_for_the_reasoning_deployment_not_16_or_32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2e-2: the FIRST real user run sent max_completion_tokens=32 for
    gpt-5-mini's smoke chat_json call -- entirely consumed by hidden reasoning tokens. The
    reasoning deployment's smoke calls must now reserve/send a much bigger bound; the
    non-reasoning deployment's calls stay unchanged (16/32)."""
    captured: dict[str, list[int]] = {"chat": [], "chat_json": []}

    def fake_post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        bound = json.get("max_completion_tokens", json.get("max_tokens"))
        key = "chat_json" if "response_format" in json else "chat"
        captured[key].append(bound)
        request = httpx.Request("POST", url)
        content = '{"ok": true}' if key == "chat_json" else "ok"
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr(sbs.httpx, "post", fake_post)
    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    sbs.run_smoke(settings, guard)

    assert captured["chat"] == [sbs.SMOKE_MAX_TOKENS, sbs.SMOKE_MAX_TOKENS_REASONING_MIN]
    assert captured["chat_json"] == [sbs.SMOKE_JSON_MAX_TOKENS, sbs.SMOKE_MAX_TOKENS_REASONING_MIN]


def test_run_smoke_records_reasoning_budget_exhausted_distinctly_not_as_a_json_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2e-2: even at the new, bigger bound, a response whose hidden
    reasoning tokens still consume the whole thing (empty content, finish_reason="length") must
    be reported as the distinct ``"reasoning_budget_exhausted"`` error type -- never an opaque
    JSONDecodeError, and ``chat_json_ok`` must be ``False``, never silently ``True`` via the
    fallback (the exact false-positive the FIRST real user run's own smoke report would have
    had, had it not crashed first)."""

    def fake_post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if "gpt-5-mini" in url:
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            )
        content = '{"ok": true}' if "response_format" in json else "ok"
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr(sbs.httpx, "post", fake_post)
    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    result = sbs.run_smoke(settings, guard)

    candidate = result[sbs.CANDIDATE_DEPLOYMENT]
    assert candidate["chat_ok"] is False
    assert candidate["chat_error_type"] == "reasoning_budget_exhausted"
    assert candidate["chat_json_ok"] is False
    assert candidate["chat_json_error_type"] == "reasoning_budget_exhausted"
    baseline = result[sbs.BASELINE_DEPLOYMENT]
    assert baseline["chat_ok"] is True
    assert baseline["chat_json_ok"] is True


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
    # DA-14.7-migration-live-2a-1: the cost guard settled DOWN to the real completion_tokens
    # (120), not left at whatever max_tokens bound was reserved.
    assert guard.log[-1]["actual_tokens_out"] == 120.0


def test_measure_token_budget_continues_after_one_prompt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2a-2: a single failed representative prompt (HTTP 500) must not
    crash this whole phase -- it records error_type/status_code for that one measurement and
    continues to the remaining prompts, exactly like run_smoke already does per call."""
    calls: list[int] = []

    def fake_post(url: str, *, json: dict[str, Any], **_kw: Any) -> httpx.Response:
        calls.append(1)
        request = httpx.Request("POST", url)
        if len(calls) == 1:
            return httpx.Response(500, request=request, json={"error": {"message": "boom"}})
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "completion_tokens": 120,
                    "completion_tokens_details": {"reasoning_tokens": 80},
                },
            },
        )

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    result = sbs.measure_token_budget(settings, guard, _fixture_dialogues(), post=fake_post)

    assert len(result["measurements"]) == 3  # router/answer/groundedness all attempted
    assert result["measurements"][0]["error_type"] == "HTTPStatusError"
    assert result["measurements"][0]["status_code"] == 500
    assert result["measurements"][0]["completion_tokens"] is None
    assert result["measurements"][1]["reasoning_tokens"] == 80


def test_measure_token_budget_tolerates_a_fully_reasoning_exhausted_response() -> None:
    """DA-14.7-migration-live-2e-2: an empty content + finish_reason="length" response is the
    EXPECTED/MEASURED outcome for this phase (its whole purpose is to detect exactly this), not
    a failure -- it must still produce a normal measurement (using `usage`), never be recorded
    under `error_type`."""

    def fake_post(_url: str, **_kw: Any) -> httpx.Response:
        request = httpx.Request("POST", "http://fake")
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {
                    "completion_tokens": 512,
                    "completion_tokens_details": {"reasoning_tokens": 512},
                },
            },
        )

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    result = sbs.measure_token_budget(settings, guard, _fixture_dialogues(), post=fake_post)

    for measurement in result["measurements"]:
        assert "error_type" not in measurement
        assert measurement["reasoning_tokens"] == 512
        assert measurement["visible_tokens"] == 0


# --- Side-by-side phase: cost guard still gates it, metrics flow through -------------------


class _FakeSession:
    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def test_run_side_by_side_reserves_per_call_not_a_flat_phase_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2a-1 fix: no more flat per-deployment reservation -- when
    ``evaluate_dialogues`` makes ZERO real Azure calls (faked here, as in part 2b's own test),
    the cost guard must not charge anything for a deployment it never actually called. Also
    covers the DA-14.7-migration-live-2a-3 fix: per_turn rows (dialogue_id/turn_index/correct/
    latency_s/tokens_in/tokens_out/eur, never reply_text) and dialogue progress counters."""
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

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    subset = sbs.azure_subset(_fixture_dialogues(), size=30)
    result = sbs.run_side_by_side(settings, guard, lambda: _FakeSession(), subset)

    assert guard.log == []  # no real Azure call was made -> nothing reserved (2a-1 fix)
    assert set(result) == {sbs.BASELINE_DEPLOYMENT, sbs.CANDIDATE_DEPLOYMENT, "flipped"}
    for deployment in (sbs.BASELINE_DEPLOYMENT, sbs.CANDIDATE_DEPLOYMENT):
        entry = result[deployment]
        assert entry["n_turns"] == 1
        assert entry["metrics"]["intent_accuracy"] == 1.0
        assert entry["dialogues_completed"] == 1
        assert entry["dialogues_total"] == 1
        row = entry["per_turn"][0]
        assert set(row) == {
            "dialogue_id",
            "turn_index",
            "correct",
            "latency_s",
            "tokens_in",
            "tokens_out",
            "eur",
        }
        assert row["dialogue_id"] == "fixture-1"
        assert row["correct"] is True
        assert "reply_text" not in row
    assert result["flipped"] == []  # identical fixture for both deployments -> nothing flipped


def test_run_side_by_side_stops_before_the_dialogue_that_exceeds_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2a-1: when the cost guard would be exceeded partway through the
    subset, run_side_by_side stops BEFORE that dialogue and reports exactly how many dialogues
    completed for the deployment it was running -- not just a generic "blocked" message."""
    calls: list[int] = []

    def fake_evaluate(
        session: Any, dialogues: list[Dialogue], chat: Any = None, top_n: Any = None
    ) -> list[DialogueTurnResult]:
        calls.append(1)
        if len(calls) == 2:  # the 2nd dialogue of the FIRST deployment's run
            raise sbs.CostCapExceeded("side-by-side: projected EUR would exceed --max-eur")
        return [
            DialogueTurnResult(
                dialogue_id=dialogues[0].id,
                turn_index=0,
                expected_intent="security",
                observed_intent="security",
                is_noisy=False,
                retrieved_docs=[],
                expected_doc=None,
                answerable=True,
                abstained=False,
                reply_text="x",
                correct=True,
                clarified=False,
                abstention_no_claims=None,
                latency_s=0.1,
            )
        ]

    monkeypatch.setattr(sbs, "evaluate_dialogues", fake_evaluate)

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    dialogues = [
        Dialogue(id=f"d{i}", turns=(Turn(message="hi", expected_intent="security", language="en"),))
        for i in range(3)
    ]

    with pytest.raises(sbs.CostCapExceeded) as excinfo:
        sbs.run_side_by_side(settings, guard, lambda: _FakeSession(), dialogues)

    partial = excinfo.value.partial_results  # type: ignore[attr-defined]
    baseline_entry = partial[sbs.BASELINE_DEPLOYMENT]
    assert baseline_entry["dialogues_completed"] == 1
    assert baseline_entry["dialogues_total"] == 3
    assert sbs.CANDIDATE_DEPLOYMENT not in partial  # never even started


def test_run_side_by_side_flipped_list_when_correctness_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2a-3: a question whose judge verdict differs between deployments
    appears in the top-level ``flipped`` list."""

    def fake_evaluate(
        session: Any, dialogues: list[Dialogue], chat: Any = None, top_n: Any = None
    ) -> list[DialogueTurnResult]:
        dialogue = dialogues[0]
        correct = chat.deployment == sbs.BASELINE_DEPLOYMENT  # baseline right, candidate wrong
        return [
            DialogueTurnResult(
                dialogue_id=dialogue.id,
                turn_index=0,
                expected_intent="security",
                observed_intent="security",
                is_noisy=False,
                retrieved_docs=[],
                expected_doc=None,
                answerable=True,
                abstained=False,
                reply_text="x",
                correct=correct,
                clarified=False,
                abstention_no_claims=None,
                latency_s=0.2,
            )
        ]

    monkeypatch.setattr(sbs, "evaluate_dialogues", fake_evaluate)

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    dialogues = [
        Dialogue(id="d0", turns=(Turn(message="hi", expected_intent="security", language="en"),))
    ]
    result = sbs.run_side_by_side(settings, guard, lambda: _FakeSession(), dialogues)

    assert result["flipped"] == [
        {
            "dialogue_id": "d0",
            "turn_index": 0,
            f"{sbs.BASELINE_DEPLOYMENT}_correct": True,
            f"{sbs.CANDIDATE_DEPLOYMENT}_correct": False,
        }
    ]


def test_run_side_by_side_isolates_a_non_cost_cap_dialogue_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-14.7-migration-live-2e-3: the ROOT CAUSE of the FIRST real user run's crash,
    reproduced directly -- a dialogue whose evaluation raises a plain RuntimeError (exactly what
    the OLD ``_deployment_from_url`` raised for a non-Azure call made through the globally
    patched ``httpx.post``) must not lose the rest of the subset or the other deployment: it is
    recorded in that deployment's ``failures`` list and the loop moves on."""

    def fake_evaluate(
        session: Any, dialogues: list[Dialogue], chat: Any = None, top_n: Any = None
    ) -> list[DialogueTurnResult]:
        if dialogues[0].id == "d1":  # the SAME dialogue fails for BOTH deployments
            raise RuntimeError(
                "cost guard: could not identify the Azure deployment from the request URL"
            )
        return [
            DialogueTurnResult(
                dialogue_id=dialogues[0].id,
                turn_index=0,
                expected_intent="security",
                observed_intent="security",
                is_noisy=False,
                retrieved_docs=[],
                expected_doc=None,
                answerable=True,
                abstained=False,
                reply_text="x",
                correct=True,
                clarified=False,
                abstention_no_claims=None,
                latency_s=0.1,
            )
        ]

    monkeypatch.setattr(sbs, "evaluate_dialogues", fake_evaluate)

    guard = _guard(max_eur=1.0)
    settings = _fake_settings()
    dialogues = [
        Dialogue(id=f"d{i}", turns=(Turn(message="hi", expected_intent="security", language="en"),))
        for i in range(3)
    ]

    result = sbs.run_side_by_side(settings, guard, lambda: _FakeSession(), dialogues)

    baseline = result[sbs.BASELINE_DEPLOYMENT]
    assert baseline["dialogues_completed"] == 2  # d0 and d2 -- d1 failed and was skipped
    assert baseline["dialogues_total"] == 3
    assert len(baseline["failures"]) == 1
    assert baseline["failures"][0]["dialogue_id"] == "d1"
    assert baseline["failures"][0]["exception_class"] == "RuntimeError"
    # the SECOND deployment's run still happened in full -- not lost either
    assert result[sbs.CANDIDATE_DEPLOYMENT]["dialogues_completed"] == 2
    assert result[sbs.CANDIDATE_DEPLOYMENT]["failures"][0]["dialogue_id"] == "d1"


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


# --- main()'s top-level safety net (DA-14.7-migration-live-2a-2, part 2d) ------------------


def test_a_smoke_phase_exception_is_isolated_and_never_leaks_the_planted_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Part 2e: a phase-level exception (an httpx.HTTPStatusError whose own str() embeds a
    planted endpoint -- exactly what a REAL Azure call's exception would do) is now isolated by
    ``_run()`` itself (DA-14.7-migration-live-2e-2/2e-3): recorded as ``smoke_error``, sanitized
    (exception CLASS + HTTP status + error code only, never str(exc)), and the run CONTINUES to
    the remaining phases instead of aborting with exit code 1 -- the exact defect the FIRST real
    user run hit (every phase's results were lost to one unhandled exception)."""
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    output_dir = tmp_path / "evidence"
    planted_endpoint = "https://PLANTED-LEAK-ENDPOINT.openai.azure.com/some/path"

    def _boom_run_smoke(*_a: Any, **_kw: Any) -> Any:
        request = httpx.Request("POST", planted_endpoint)
        response = httpx.Response(500, request=request, json={"error": {"code": "boom_code"}})
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(sbs, "run_smoke", _boom_run_smoke)
    monkeypatch.setattr(sbs, "get_settings", lambda: _fake_settings())
    # Isolate this test to the smoke phase only -- phase (b) is a no-op fixture here (it is not
    # what this test is about), and --smoke-only below skips phase (c)/the DB entirely.
    monkeypatch.setattr(
        sbs,
        "measure_token_budget",
        lambda *_a, **_kw: {"measurements": [], "recommended_max_tokens": None},
    )

    exit_code = sbs.main(
        ["--smoke-only", "--prices", str(prices_path), "--output-dir", str(output_dir)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0  # phase isolation: a smoke failure no longer aborts the whole run
    blob = captured.out + captured.err
    assert planted_endpoint not in blob
    assert "HTTPStatusError" in blob
    assert "500" in blob

    evidence_text = (output_dir / "summary.md").read_text(encoding="utf-8")
    results_text = (output_dir / "results.json").read_text(encoding="utf-8")
    assert planted_endpoint not in evidence_text
    assert planted_endpoint not in results_text
    assert "HTTPStatusError" in evidence_text

    results = json.loads(results_text)
    assert results["smoke_error"]["exception_class"] == "HTTPStatusError"
    assert results["smoke_error"]["status_code"] == 500
    assert results["smoke_error"]["error_code"] == "boom_code"
    assert "token_budget" in results  # phase (b) still ran -- not lost by phase (a)'s failure


def test_main_last_resort_handler_still_sanitizes_a_failure_before_any_phase_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()``'s own outermost try/except is now only a true last resort (part 2e): it still
    fires, still sanitizes, and still exits non-zero for a failure BEFORE any phase's isolation
    even exists yet (e.g. ``get_settings`` itself)."""
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    output_dir = tmp_path / "evidence"
    planted_endpoint = "https://PLANTED-LEAK-ENDPOINT.openai.azure.com/some/path"

    def _boom_get_settings(*_a: Any, **_kw: Any) -> Any:
        request = httpx.Request("POST", planted_endpoint)
        response = httpx.Response(500, request=request, json={"error": "boom"})
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(sbs, "get_settings", _boom_get_settings)
    # Part 2f: the preflight (DB/Ollama) now runs BEFORE get_settings() whenever phase (c) is
    # going to run -- stub it to "pass" here so this test still exercises get_settings()
    # itself failing (the preflight's own behaviour has its own dedicated tests below).
    monkeypatch.setattr(
        sbs,
        "run_preflight",
        lambda: [sbs.PreflightCheck("db", True, "ok"), sbs.PreflightCheck("ollama", True, "ok")],
    )

    exit_code = sbs.main(["--prices", str(prices_path), "--output-dir", str(output_dir)])
    captured = capsys.readouterr()

    assert exit_code == 1
    blob = captured.out + captured.err
    assert planted_endpoint not in blob
    assert "HTTPStatusError" in blob
    assert "500" in blob

    evidence_text = (output_dir / "summary.md").read_text(encoding="utf-8")
    results_text = (output_dir / "results.json").read_text(encoding="utf-8")
    assert planted_endpoint not in evidence_text
    assert planted_endpoint not in results_text
    assert "HTTPStatusError" in evidence_text


# --- End-to-end reproduction of the FIRST real user run (part 2e) --------------------------


def test_main_end_to_end_reproduces_and_recovers_from_the_first_real_user_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end reproduction of the EXACT first real user run (2026-11-28 01:48,
    ``bash scripts/azure/openai_live_check.sh --max-eur 0.30``, no ``--smoke-only``): gpt-5-mini's
    hidden reasoning tokens exhaust the smoke/token-budget bounds (empty content,
    ``finish_reason="length"``), AND phase (c)'s real retrieval makes a non-Azure ``httpx.post``
    call (simulating Ollama's ``/api/embed``) through the globally patched ``httpx.post``. The
    OLD code raised an unhandled ``RuntimeError`` at exactly that point and lost every phase's
    results -- the real run's own ``results.json`` was just
    ``{"unhandled_error": {"exception_class": "RuntimeError", "status_code": null}}``. This test
    proves the fix: ``main()`` now returns 0 and ``results.json`` has complete per-deployment
    smoke/token-budget/side-by-side entries."""
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    output_dir = tmp_path / "evidence"

    def fake_post(url: str, *, json: dict[str, Any] | None = None, **_kw: Any) -> httpx.Response:
        body = json or {}
        request = httpx.Request("POST", url)
        if "gpt-5-mini" in url:
            bound = body.get("max_completion_tokens", 0)
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                    "usage": {
                        "completion_tokens": bound,
                        "completion_tokens_details": {"reasoning_tokens": bound},
                    },
                },
            )
        content = '{"ok": true}' if "response_format" in body else "ok"
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr(sbs.httpx, "post", fake_post)
    monkeypatch.setattr(sbs, "get_settings", lambda: _fake_settings())
    monkeypatch.setattr(sbs, "load_dialogues", lambda: _fixture_dialogues())

    def fake_evaluate_dialogues(
        session: Any, dialogues: list[Dialogue], chat: Any = None, top_n: Any = None
    ) -> list[DialogueTurnResult]:
        # THE ROOT CAUSE, reproduced directly: a non-Azure httpx.post call (Ollama's own
        # /api/embed, made by real retrieval) through the SAME globally patched httpx.post
        # phase (c) installs -- must NOT raise (DA-14.7-migration-live-2e-1 fix).
        httpx.post(
            "http://localhost:11434/api/embed",
            json={"model": "bge-m3", "input": ["hello"]},
            timeout=170,
        )
        return [
            DialogueTurnResult(
                dialogue_id=dialogues[0].id,
                turn_index=0,
                expected_intent="security",
                observed_intent="security",
                is_noisy=False,
                retrieved_docs=[],
                expected_doc=None,
                answerable=True,
                abstained=False,
                reply_text="x",
                correct=True,
                clarified=False,
                abstention_no_claims=None,
                latency_s=0.05,
            )
        ]

    monkeypatch.setattr(sbs, "evaluate_dialogues", fake_evaluate_dialogues)
    monkeypatch.setattr(
        "rag_app.db.session.make_session_factory",
        lambda engine=None: (lambda: _FakeSession()),
    )
    # Part 2f: the preflight (DB/Ollama) now runs BEFORE phase (a) -- stub it to "pass" so this
    # test still reproduces the ORIGINAL first-real-user-run failure (phase (c)'s non-Azure
    # httpx.post call), not a DB/Ollama preflight failure (which has its own dedicated tests).
    monkeypatch.setattr(
        sbs,
        "run_preflight",
        lambda: [sbs.PreflightCheck("db", True, "ok"), sbs.PreflightCheck("ollama", True, "ok")],
    )

    exit_code = sbs.main(
        ["--max-eur", "0.30", "--prices", str(prices_path), "--output-dir", str(output_dir)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0  # the real run's exit code was 1 (unhandled RuntimeError)
    assert "ERROR: unhandled" not in captured.err

    results = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    assert "unhandled_error" not in results  # the exact (and ONLY) field the real run's had

    smoke = results["smoke"]
    assert smoke[sbs.CANDIDATE_DEPLOYMENT]["chat_json_error_type"] == "reasoning_budget_exhausted"
    assert smoke[sbs.BASELINE_DEPLOYMENT]["chat_json_ok"] is True

    assert results["token_budget"]["measurements"]  # phase (b) ran and produced data

    side_by_side = results["side_by_side"]
    for deployment in (sbs.BASELINE_DEPLOYMENT, sbs.CANDIDATE_DEPLOYMENT):
        assert side_by_side[deployment]["dialogues_completed"] == 1
        assert side_by_side[deployment]["failures"] == []


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


# --- part 2f: the DB/Ollama preflight (offline fakes only, F43) ----------------------------


class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakePreflightSession:
    def __init__(self, scalars: list[Any]) -> None:
        self._scalars = list(scalars)

    def __enter__(self) -> _FakePreflightSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, _stmt: Any) -> _FakeScalarResult:
        return _FakeScalarResult(self._scalars.pop(0))


def _patch_db_session_factory(monkeypatch: pytest.MonkeyPatch, scalars: list[Any]) -> None:
    monkeypatch.setattr(
        "rag_app.db.session.make_session_factory",
        lambda engine=None: (lambda: _FakePreflightSession(scalars)),
    )


class _FakeEmbedder:
    vector: list[float] | None = None
    error: Exception | None = None

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    def embed_one(self, _text: str) -> list[float]:
        if self.error is not None:
            raise self.error
        assert self.vector is not None
        return self.vector


def _patch_ollama_embedder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vector: list[float] | None = None,
    error: Exception | None = None,
) -> None:
    fake_cls = type("_FakeEmbedderInstance", (_FakeEmbedder,), {"vector": vector, "error": error})
    monkeypatch.setattr("rag_app.embeddings.OllamaEmbedder", fake_cls)


def test_preflight_db_ok_reports_chunk_count_and_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    from rag_app.db.models import EMBEDDING_DIM

    _patch_db_session_factory(monkeypatch, [42, EMBEDDING_DIM])
    check = sbs.preflight_db()
    assert check.name == "db"
    assert check.ok is True
    assert "42" in check.detail
    assert str(EMBEDDING_DIM) in check.detail


def test_preflight_db_fails_closed_on_empty_chunks_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db_session_factory(monkeypatch, [0])
    check = sbs.preflight_db()
    assert check.ok is False
    assert "empty" in check.detail


def test_preflight_db_fails_on_embedding_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_app.db.models import EMBEDDING_DIM

    _patch_db_session_factory(monkeypatch, [10, EMBEDDING_DIM - 1])
    check = sbs.preflight_db()
    assert check.ok is False
    assert str(EMBEDDING_DIM) in check.detail
    assert str(EMBEDDING_DIM - 1) in check.detail


def test_preflight_db_reports_not_reachable_on_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    class _BoomSession:
        def __enter__(self) -> _BoomSession:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def execute(self, _stmt: Any) -> Any:
            raise OperationalError("connect failed", {}, Exception("connection refused"))

    monkeypatch.setattr(
        "rag_app.db.session.make_session_factory", lambda engine=None: (lambda: _BoomSession())
    )
    check = sbs.preflight_db()
    assert check.ok is False
    assert "not reachable" in check.detail
    assert "OperationalError" in check.detail


def test_preflight_db_reports_session_factory_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(engine: Any = None) -> Any:
        raise ValueError("bad DATABASE_URL")

    monkeypatch.setattr("rag_app.db.session.make_session_factory", _boom)
    check = sbs.preflight_db()
    assert check.ok is False
    assert "ValueError" in check.detail


def test_preflight_ollama_ok_reports_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    from rag_app.db.models import EMBEDDING_DIM

    _patch_ollama_embedder(monkeypatch, vector=[0.1] * EMBEDDING_DIM)
    check = sbs.preflight_ollama()
    assert check.name == "ollama"
    assert check.ok is True
    assert str(EMBEDDING_DIM) in check.detail


def test_preflight_ollama_reports_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "http://localhost:11434/api/embed")
    _patch_ollama_embedder(monkeypatch, error=httpx.ConnectError("boom", request=request))
    check = sbs.preflight_ollama()
    assert check.ok is False
    assert "not reachable" in check.detail
    assert "ConnectError" in check.detail


def test_preflight_ollama_fails_on_embedding_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_app.db.models import EMBEDDING_DIM

    _patch_ollama_embedder(monkeypatch, vector=[0.1] * (EMBEDDING_DIM - 1))
    check = sbs.preflight_ollama()
    assert check.ok is False
    assert str(EMBEDDING_DIM - 1) in check.detail


def test_run_preflight_runs_both_checks_even_if_the_first_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sbs, "preflight_db", lambda: sbs.PreflightCheck("db", False, "down"))
    monkeypatch.setattr(sbs, "preflight_ollama", lambda: sbs.PreflightCheck("ollama", True, "ok"))
    checks = sbs.run_preflight()
    assert [c.name for c in checks] == ["db", "ollama"]
    assert checks[0].ok is False
    assert checks[1].ok is True


def test_format_preflight_prints_ok_and_fail_distinctly() -> None:
    lines = sbs.format_preflight(
        [sbs.PreflightCheck("db", True, "42 chunks"), sbs.PreflightCheck("ollama", False, "down")]
    )
    assert any("db" in line and "OK" in line and "42 chunks" in line for line in lines)
    assert any("ollama" in line and "FAIL" in line and "down" in line for line in lines)


def test_main_preflight_only_flag_runs_checks_and_never_touches_azure_or_cost_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("--preflight-only must never touch settings, prices or the network")

    monkeypatch.setattr(sbs, "get_settings", _boom)
    monkeypatch.setattr(sbs, "resolve_prices_path", _boom)
    monkeypatch.setattr(sbs.httpx, "post", _boom)
    monkeypatch.setattr(
        sbs,
        "run_preflight",
        lambda: [
            sbs.PreflightCheck("db", True, "7 chunks"),
            sbs.PreflightCheck("ollama", True, "ok"),
        ],
    )

    exit_code = sbs.main(["--preflight-only"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "preflight db: OK" in captured.out
    assert "preflight ollama: OK" in captured.out


def test_main_preflight_only_flag_returns_nonzero_on_any_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sbs,
        "run_preflight",
        lambda: [
            sbs.PreflightCheck("db", False, "empty"),
            sbs.PreflightCheck("ollama", True, "ok"),
        ],
    )
    assert sbs.main(["--preflight-only"]) == 1


def test_run_refuses_any_azure_call_when_preflight_fails_and_not_smoke_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    output_dir = tmp_path / "evidence"

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("a failed preflight must never let any Azure call happen")

    monkeypatch.setattr(sbs, "get_settings", _boom)
    monkeypatch.setattr(sbs.httpx, "post", _boom)
    monkeypatch.setattr(
        sbs,
        "run_preflight",
        lambda: [
            sbs.PreflightCheck("db", False, "the chunks table is empty"),
            sbs.PreflightCheck("ollama", True, "ok"),
        ],
    )

    exit_code = sbs.main(["--prices", str(prices_path), "--output-dir", str(output_dir)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "preflight failed" in (captured.out + captured.err).lower()
    assert "the chunks table is empty" in captured.err

    results = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    assert "preflight_failed" in results
    assert results["preflight_failed"][0]["name"] == "db"
    assert results["preflight_failed"][0]["ok"] is False


def test_run_skips_preflight_entirely_for_smoke_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> list[sbs.PreflightCheck]:
        raise AssertionError("--smoke-only must never run the DB/Ollama preflight")

    monkeypatch.setattr(sbs, "run_preflight", _boom)
    monkeypatch.setattr(sbs, "get_settings", lambda: _fake_settings())
    monkeypatch.setattr(sbs, "load_dialogues", lambda: _fixture_dialogues())

    def fake_post(url: str, *, json: dict[str, Any] | None = None, **_kw: Any) -> httpx.Response:
        body = json or {}
        request = httpx.Request("POST", url)
        content = '{"ok": true}' if "response_format" in body else "ok"
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr(sbs.httpx, "post", fake_post)

    exit_code = sbs.main(["--smoke-only", "--prices", str(_write_fake_prices_file(monkeypatch))])
    assert exit_code == 0


def _write_fake_prices_file(monkeypatch: pytest.MonkeyPatch) -> Path:
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text(json.dumps(FAKE_PRICES), encoding="utf-8")
    monkeypatch.setattr(sbs, "REPO_ROOT", path.parent)
    return path
