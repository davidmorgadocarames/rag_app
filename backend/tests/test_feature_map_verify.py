"""Tests for ``scripts/dev/verify.sh`` (Harness-B-fix, DA-HB-1/DA-HB-2).

DA-HB-1: ``blocked()`` must write the FULL message (every argument, not just the first) to
``verdict.log`` -- a call site splitting its reason across several quoted arguments (for
readability) must not get silently truncated in the evidence this tool exists to produce.

DA-HB-2: ``--base-url`` (an ALREADY RUNNING, possibly remote/Azure app) must refuse any
feature whose covering e2e spec(s) are marked ``"mutating"`` in ``FEATURE_DOCS``
(``backend/src/rag_app/features.py`` -- account creation, e-mail sending, erasure, lab
runs...) unless ``--allow-remote-writes`` is explicitly passed. A ``"read-only"`` feature is
allowed through the same gate with no flag. The local bring-up path (no ``--base-url``) is
unaffected either way.

Every test runs the real script as a subprocess against an UNREACHABLE/fake URL
(``http://127.0.0.1:1`` -- nothing listens on a privileged port locally, so `curl` fails
instantly with no real network call ever reaching anything) so the mutation gate itself is
exercised without ever actually running Playwright or touching a real remote target.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERIFY = _REPO_ROOT / "scripts" / "dev" / "verify.sh"
_UNREACHABLE_URL = "http://127.0.0.1:1"  # nothing listens here; curl fails instantly


def _resolve_verify_sh_venv_python() -> Path:
    """Mirrors `scripts/dev/verify.sh`'s OWN venv resolution (GATE_VENV, else
    `<main-root>/backend/.venv`) so the skip check below asks exactly the same question the
    script itself will ask -- never a looser/different one that could pass here and still
    fail inside the subprocess."""
    gate_venv = os.environ.get("GATE_VENV")
    if gate_venv:
        return Path(gate_venv) / "bin" / "python"
    common_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    main_root = Path(common_dir).parent if common_dir else _REPO_ROOT
    return main_root / "backend" / ".venv" / "bin" / "python"


_VENV_PYTHON = _resolve_verify_sh_venv_python()

# verify.sh itself BLOCKs (before ever creating an evidence directory) if `jq`/`curl`/`npx`
# are not on PATH, or if its own venv does not resolve -- a CI job that runs backend pytest
# straight against the system interpreter (no `backend/.venv`, no Node/npx at all, e.g. this
# repo's "Backend (lint, types, tests incl. DB)" job) hits that BEFORE this test's own
# assertions ever run, which would otherwise surface as a confusing FileNotFoundError instead
# of a clean skip.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None
    or shutil.which("jq") is None
    or shutil.which("curl") is None
    or shutil.which("npx") is None
    or not os.access(_VENV_PYTHON, os.X_OK),
    reason="needs bash/jq/curl/npx on PATH and a resolvable backend venv"
    " (scripts/dev/verify.sh's own prerequisites)",
)


def _run(feature_id: str, *extra_args: str, evidence_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SECRAG_VERIFY_EVIDENCE_DIR"] = str(evidence_dir)
    return subprocess.run(
        ["bash", str(_VERIFY), feature_id, *extra_args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _verdict_log(evidence_dir: Path, feature_id: str) -> str:
    """The one run directory verify.sh created for this feature, under evidence_dir."""
    runs = sorted((evidence_dir / feature_id).iterdir())
    assert runs, f"no evidence directory written for {feature_id}"
    return (runs[-1] / "verdict.log").read_text(encoding="utf-8")


# --- DA-HB-1: blocked() must not truncate a multi-argument message -------------------------


def test_blocked_message_is_the_full_sentence_not_just_the_first_argument(tmp_path: Path) -> None:
    """``stack-health-endpoint`` has no covering e2e spec, so verify.sh calls
    ``blocked()`` with its reason split across 3 quoted segments (the exact shape DA-HB-1
    flagged as truncated on the old `$1`-only code). The full sentence -- including the
    probe-fallback hint and the `registry_check` suggestion, both PAST the first segment --
    must land in verdict.log."""
    result = _run("stack-health-endpoint", evidence_dir=tmp_path)
    assert result.returncode == 2
    verdict = _verdict_log(tmp_path, "stack-health-endpoint")
    assert verdict.startswith("BLOCKED: no covering e2e spec for 'stack-health-endpoint'")
    # Everything past the first segment (truncated on the old code) must be present too.
    assert "this feature is checked by its own probe instead:" in verdict
    assert "run 'python -m rag_app.devtools.registry_check'" in verdict


def test_negative_control_the_truncated_old_form_is_not_what_lands_in_the_log(
    tmp_path: Path,
) -> None:
    """Negative control: the OLD (buggy, `$1`-only) ``blocked()`` would have written exactly
    the first segment and nothing else. Prove that truncated string is NOT the full content
    of verdict.log today -- if it were, the positive test above could pass vacuously on a
    verdict.log that simply happens to start with the same words."""
    result = _run("stack-health-endpoint", evidence_dir=tmp_path)
    assert result.returncode == 2
    verdict = _verdict_log(tmp_path, "stack-health-endpoint")
    truncated_old_form = (
        "BLOCKED: no covering e2e spec for 'stack-health-endpoint' "
        "(docs/FEATURE_MAP.md's coverage table) --\n"
    )
    assert verdict != truncated_old_form
    assert len(verdict) > len(truncated_old_form)


# --- DA-HB-2: the remote mutation gate ------------------------------------------------------


def test_remote_mutating_feature_without_the_flag_is_blocked(tmp_path: Path) -> None:
    """``identity-mfa`` is marked "mutating" (its covering specs enrol real MFA / erase a
    real account). Against --base-url with no --allow-remote-writes, it must BLOCKED (exit 2)
    with a reason naming the mutation refusal -- and never even attempt the curl reachability
    probe (the fake URL below is never actually dialled for this reason)."""
    result = _run("identity-mfa", "--base-url", _UNREACHABLE_URL, evidence_dir=tmp_path)
    assert result.returncode == 2
    verdict = _verdict_log(tmp_path, "identity-mfa")
    assert "MUTATING" in verdict
    assert "--allow-remote-writes" in verdict
    assert _UNREACHABLE_URL in verdict
    # The curl probe (a DIFFERENT BLOCKED reason, and the only thing that would try a real
    # network call) must never have been reached -- the mutation gate runs first.
    assert "Could not connect" not in (result.stdout + result.stderr)


def test_remote_mutating_feature_with_the_flag_prints_a_warning_and_passes_the_gate(
    tmp_path: Path,
) -> None:
    """With --allow-remote-writes, the mutation gate must let the run through -- it still
    ends up BLOCKED because the fake URL is unreachable, but for THAT reason (the
    reachability probe), never the mutation refusal, and only after printing a clear
    warning."""
    result = _run(
        "identity-mfa",
        "--base-url",
        _UNREACHABLE_URL,
        "--allow-remote-writes",
        evidence_dir=tmp_path,
    )
    assert result.returncode == 2
    verdict = _verdict_log(tmp_path, "identity-mfa")
    assert "MUTATING" not in verdict
    assert "not reachable" in verdict
    combined_output = result.stdout + result.stderr
    assert "WARNING" in combined_output
    assert "MUTATING spec against a REMOTE target" in combined_output


def test_remote_read_only_feature_is_allowed_past_the_mutation_gate(tmp_path: Path) -> None:
    """``stack-coldstart-warmup`` is "read-only" (its covering specs only log in with an
    already-seeded account). Against --base-url with NO --allow-remote-writes, it must pass
    the mutation gate with no flag needed -- it still ends up BLOCKED because the fake URL is
    unreachable, but for that unrelated reason, never a mutation refusal."""
    result = _run("stack-coldstart-warmup", "--base-url", _UNREACHABLE_URL, evidence_dir=tmp_path)
    assert result.returncode == 2
    verdict = _verdict_log(tmp_path, "stack-coldstart-warmup")
    assert "MUTATING" not in verdict
    assert "not reachable" in verdict


def test_local_path_is_unchanged_by_the_new_flag(tmp_path: Path) -> None:
    """No --base-url at all (the local bring-up path): a feature with no covering e2e spec
    is BLOCKED with the SAME "no covering e2e spec" reason regardless of whether
    --allow-remote-writes is passed -- the new flag only has meaning on the --base-url path,
    proving the local path's own behaviour (and argument parsing) is unchanged."""
    without_flag = _run("stack-health-endpoint", evidence_dir=tmp_path / "a")
    with_flag = _run("stack-health-endpoint", "--allow-remote-writes", evidence_dir=tmp_path / "b")
    assert without_flag.returncode == with_flag.returncode == 2
    verdict_without = _verdict_log(tmp_path / "a", "stack-health-endpoint")
    verdict_with = _verdict_log(tmp_path / "b", "stack-health-endpoint")
    assert verdict_without == verdict_with
    assert "no covering e2e spec" in verdict_without
