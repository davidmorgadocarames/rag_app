"""Unit tests for the ``registry`` gate step's quality-probe check (T13.0.5, T14.5.3/DEBT-049).

No DB/live app needed: :func:`check_quality_probes` is exercised against throwaway
``FeatureEntry`` tuples and a monkeypatched ``QUALITY_PROBES`` dict, never the real
``rag_app.features.REGISTRY``/``QUALITY_PROBES``.
"""

from __future__ import annotations

import pytest

from rag_app.devtools import registry_check
from rag_app.features import FeatureEntry, QualityProbeResult


def _entry(**kwargs: object) -> FeatureEntry:
    defaults: dict[str, object] = {
        "id": "test-entry",
        "name": "Test entry",
        "phase": "14",
        "status": "enabled",
        "docs": "docs/TRD.md",
    }
    defaults.update(kwargs)
    return FeatureEntry(**defaults)  # type: ignore[arg-type]


def test_must_pass_probe_failing_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    """T14.5.3: fails on the old code -- before this row, a 'failed' outcome on an 'enabled'
    entry's quality probe was never added to ``problems`` at all, regardless of
    ``must_pass_quality_probes`` (which did not exist)."""
    monkeypatch.setitem(
        registry_check.QUALITY_PROBES,
        "fake-probe",
        lambda _session: QualityProbeResult("failed", "regressed"),
    )
    entries = (_entry(quality_probes=("fake-probe",), must_pass_quality_probes=("fake-probe",)),)

    problems = registry_check.check_quality_probes(entries, session=None)

    assert len(problems) == 1
    assert "fake-probe" in problems[0]
    assert "must-pass" in problems[0]


def test_must_pass_probe_passing_is_not_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        registry_check.QUALITY_PROBES,
        "fake-probe",
        lambda _session: QualityProbeResult("passed", "ok"),
    )
    entries = (_entry(quality_probes=("fake-probe",), must_pass_quality_probes=("fake-probe",)),)

    problems = registry_check.check_quality_probes(entries, session=None)

    assert problems == []


def test_a_failing_probe_not_listed_as_must_pass_is_not_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEBT-049's own precedent: ``misspelling-tolerance-variants`` can keep failing a known,
    tracked subset without ever blocking the gate -- this is the general mechanism behind
    that specific decision, proven against a throwaway entry/probe."""
    monkeypatch.setitem(
        registry_check.QUALITY_PROBES,
        "fake-probe",
        lambda _session: QualityProbeResult("failed", "a known, tracked gap"),
    )
    entries = (_entry(quality_probes=("fake-probe",)),)  # not in must_pass_quality_probes

    problems = registry_check.check_quality_probes(entries, session=None)

    assert problems == []


def test_a_skipped_probe_is_never_a_problem_even_when_must_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live DB (the fast gate's normal case) -- every probe reports 'skipped', never
    'failed', so a must-pass probe never blocks a DB-less run."""
    monkeypatch.setitem(
        registry_check.QUALITY_PROBES,
        "fake-probe",
        lambda _session: QualityProbeResult("skipped", "no live DB in this run"),
    )
    entries = (_entry(quality_probes=("fake-probe",), must_pass_quality_probes=("fake-probe",)),)

    problems = registry_check.check_quality_probes(entries, session=None)

    assert problems == []


def test_expected_fail_entry_whose_probe_now_passes_is_still_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing T13.0.5 rule, unaffected by T14.5.3's addition -- still enforced via the
    same function after the refactor."""
    monkeypatch.setitem(
        registry_check.QUALITY_PROBES,
        "fake-probe",
        lambda _session: QualityProbeResult("passed", "now passes"),
    )
    entries = (_entry(status="expected-fail", quality_probes=("fake-probe",)),)

    problems = registry_check.check_quality_probes(entries, session=None)

    assert len(problems) == 1
    assert "expected-fail" in problems[0]


def test_both_rules_can_fire_independently_on_the_same_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry could in principle be both 'expected-fail' AND must-pass (not a real registry
    entry today, but the two checks are independent, not mutually exclusive) -- a 'failed'
    outcome only ever trips the must-pass rule, never the expected-fail one (which only
    fires on 'passed')."""
    monkeypatch.setitem(
        registry_check.QUALITY_PROBES,
        "fake-probe",
        lambda _session: QualityProbeResult("failed", "still failing"),
    )
    entries = (
        _entry(
            status="expected-fail",
            quality_probes=("fake-probe",),
            must_pass_quality_probes=("fake-probe",),
        ),
    )

    problems = registry_check.check_quality_probes(entries, session=None)

    assert len(problems) == 1
    assert "must-pass" in problems[0]


def test_misspelling_tolerance_entry_is_the_only_must_pass_probe_in_the_real_registry() -> None:
    """Pins the exact, deliberate scope of T14.5.3/DEBT-049 against the REAL registry: only
    the single-item probe is must-pass, never the harder variants probe."""
    from rag_app.features import REGISTRY

    entry = next(e for e in REGISTRY if e.id == "quality-misspelling-probe")
    assert entry.must_pass_quality_probes == ("misspelling-tolerance",)
    assert "misspelling-tolerance-variants" not in entry.must_pass_quality_probes
