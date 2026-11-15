"""Unit tests for the feature registry (T13.0.2, P13-D3): entry shape, health-probe
timeout/graceful-degradation, the "expected-fail" quality probe, and the ``check_registry``
cross-checks the ``registry`` gate step (``rag_app.devtools.registry_check``) runs.

No DB/Ollama needed: every health probe is designed to answer within 2 s even with nothing
reachable (T13.0.4), and the "a new router without an entry fails the gate" test builds its
own throwaway app rather than depending on a live stack.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, FastAPI

from rag_app import metrics
from rag_app.api.app import create_app
from rag_app.features import (
    QUALITY_PROBES,
    REGISTRY,
    FeatureEntry,
    HealthProbeResult,
    check_registry,
    module_probe,
    quality_probe_misspelling,
    route_probe,
    run_health_probe,
)

# --- registry shape ----------------------------------------------------------------------


def test_every_entry_has_a_unique_id() -> None:
    ids = [entry.id for entry in REGISTRY]
    assert len(ids) == len(set(ids))


def test_every_entry_has_a_non_empty_docs_link() -> None:
    for entry in REGISTRY:
        assert entry.docs, entry.id


def test_every_entry_status_is_one_of_the_four_values() -> None:
    allowed = {"enabled", "disabled", "retained-not-adopted", "expected-fail"}
    for entry in REGISTRY:
        assert entry.status in allowed, entry.id


def test_the_misspelling_entry_is_the_only_expected_fail() -> None:
    expected_fail = [e for e in REGISTRY if e.status == "expected-fail"]
    assert [e.id for e in expected_fail] == ["quality-misspelling-probe"]


def test_the_agentic_router_is_retained_not_adopted() -> None:
    entry = next(e for e in REGISTRY if e.id == "rag-agentic-router")
    assert entry.status == "retained-not-adopted"


def test_every_expected_fail_entry_names_a_registered_quality_probe() -> None:
    """Only an `expected-fail` entry's quality probe is actually RUN (to prove it still
    fails, T13.0.5) -- every other entry's `quality_probes` is documentation only (which
    gate step/golden-set measures it), so it need not resolve to a callable here."""
    for entry in REGISTRY:
        if entry.status != "expected-fail":
            continue
        assert entry.quality_probes
        for probe_id in entry.quality_probes:
            assert probe_id in QUALITY_PROBES, (entry.id, probe_id)


# --- run_health_probe: the ≤ 2s deadline, always, never a crash --------------------------


def test_run_health_probe_enforces_the_deadline() -> None:
    def _hangs() -> HealthProbeResult:
        time.sleep(10)
        return HealthProbeResult("ok", "should never get here")

    start = time.monotonic()
    result = run_health_probe(_hangs)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0  # well under the probe's own sleep(10)
    assert result.status == "down"
    assert "timed out" in result.detail


def test_run_health_probe_never_raises() -> None:
    def _boom() -> HealthProbeResult:
        raise RuntimeError("boom")

    result = run_health_probe(_boom)
    assert result.status == "down"
    assert "RuntimeError" in result.detail


def test_every_health_probe_answers_within_budget() -> None:
    """Every registered health probe, run for real (no live stack assumed) — each must
    answer (``ok``/``degraded``/``down``), never hang or crash, well inside the 2 s budget
    ``run_health_probe`` enforces structurally."""
    for entry in REGISTRY:
        if entry.health_probe is None:
            continue
        result = run_health_probe(entry.health_probe)
        assert result.status in ("ok", "degraded", "down"), entry.id
        assert result.detail


def test_module_probe_reports_down_for_a_missing_module() -> None:
    probe = module_probe("rag_app.this_module_does_not_exist")
    assert run_health_probe(probe).status == "down"


def test_module_probe_reports_down_for_a_missing_attribute() -> None:
    probe = module_probe("rag_app.metrics", "this_attr_does_not_exist")
    assert run_health_probe(probe).status == "down"


def test_route_probe_finds_a_real_route() -> None:
    probe = route_probe("GET", "/health")
    assert run_health_probe(probe).status == "ok"


def test_route_probe_reports_down_for_an_unknown_route() -> None:
    probe = route_probe("GET", "/this/route/does/not/exist")
    assert run_health_probe(probe).status == "down"


# --- the misspelling quality probe: skipped with no stack, never a crash -----------------


def test_quality_probe_misspelling_skips_with_no_session() -> None:
    result = quality_probe_misspelling(None)
    assert result.outcome == "skipped"


def test_misspell_question_changes_the_longest_word() -> None:
    from rag_app.features import _misspell_question

    original = "What is the password reset policy for administrators"
    typo = _misspell_question(original)
    assert typo != original
    assert len(typo.split()) == len(original.split())


# --- check_registry: structural cross-checks ---------------------------------------------


def test_check_registry_has_no_problems_against_the_real_app_and_metrics() -> None:
    """The registry is complete TODAY: every tag the real app's routes use is claimed, and
    every declared metric exists in the real Prometheus registry."""
    app = create_app()
    problems = check_registry(app, metrics.REGISTRY)
    assert problems == []


def test_a_new_router_with_an_unclaimed_tag_fails_the_check() -> None:
    """Done-when (T13.0.5): "a new router without an entry fails the gate" — proven here
    with a throwaway app/router rather than depending on anyone actually forgetting to
    register something in the real app."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/totally-new-thing", tags=["brand-new-unclaimed-tag"])
    def _new_route() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(router)
    problems = check_registry(app, metrics.REGISTRY)
    assert any("brand-new-unclaimed-tag" in problem for problem in problems)


def test_a_declared_metric_that_does_not_exist_fails_the_check() -> None:
    bogus_entry = FeatureEntry(
        id="test-only-bogus-entry",
        name="bogus",
        phase="13",
        status="enabled",
        docs="docs/TRD.md",
        metrics=("secrag_this_metric_does_not_exist_total",),
    )
    app = create_app()
    problems = check_registry(app, metrics.REGISTRY, entries=(bogus_entry,))
    assert any("secrag_this_metric_does_not_exist_total" in problem for problem in problems)


def test_a_declared_module_that_does_not_exist_fails_the_check() -> None:
    bogus_entry = FeatureEntry(
        id="test-only-bogus-entry-2",
        name="bogus",
        phase="13",
        status="enabled",
        docs="docs/TRD.md",
        modules=("rag_app.this_module_does_not_exist",),
    )
    app = create_app()
    problems = check_registry(app, metrics.REGISTRY, entries=(bogus_entry,))
    assert any("rag_app.this_module_does_not_exist" in problem for problem in problems)


def test_expected_fail_without_a_quality_probe_fails_the_check() -> None:
    bogus_entry = FeatureEntry(
        id="test-only-bogus-entry-3",
        name="bogus",
        phase="13",
        status="expected-fail",
        docs="docs/TRD.md",
    )
    app = create_app()
    problems = check_registry(app, metrics.REGISTRY, entries=(bogus_entry,))
    assert any("no quality_probes listed" in problem for problem in problems)
