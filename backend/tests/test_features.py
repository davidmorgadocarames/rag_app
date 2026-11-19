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
    check_dod_compliance,
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


def test_there_is_no_expected_fail_entry_today() -> None:
    """DEC-037 = A / DEBT-049 (13-E): `quality-misspelling-probe` was the only
    `expected-fail` entry; it flipped to `enabled` once the live-DB probe genuinely passed
    (verified, not an artefact -- see ``quality_probe_misspelling``'s docstring). Nothing
    else is registered `expected-fail` today."""
    expected_fail = [e for e in REGISTRY if e.status == "expected-fail"]
    assert expected_fail == []


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


def test_quality_probe_misspelling_variants_skips_with_no_session() -> None:
    """DEBT-049: same no-stack contract as the single-item probe."""
    from rag_app.features import quality_probe_misspelling_variants

    result = quality_probe_misspelling_variants(None)
    assert result.outcome == "skipped"


def test_misspelling_tolerance_variants_is_registered() -> None:
    assert "misspelling-tolerance-variants" in QUALITY_PROBES
    entry = next(e for e in REGISTRY if e.id == "quality-misspelling-probe")
    assert entry.status == "enabled"
    assert "misspelling-tolerance" in entry.quality_probes
    assert "misspelling-tolerance-variants" in entry.quality_probes


def test_misspell_question_changes_the_longest_word() -> None:
    from rag_app.features import _misspell_question

    original = "What is the password reset policy for administrators"
    typo = _misspell_question(original)
    assert typo != original
    assert len(typo.split()) == len(original.split())


def test_each_typo_strategy_changes_a_long_enough_word() -> None:
    """DEBT-049: every strategy from easiest to hardest actually mutates a word with room
    for it (``len >= 4``) -- a strategy that silently no-ops would make the variants probe
    report a false "tolerated" (the ``typo_question == item.question`` skip exists in the
    probe precisely to avoid that, but the strategies themselves must still do something)."""
    from rag_app.features import _TYPO_STRATEGIES

    word = "injection"
    for name, strategy in _TYPO_STRATEGIES.items():
        mutated = strategy(word)
        assert mutated != word, name
        assert len(mutated) >= len(word) - 1, name  # deletion is the only length-shrinking one


def test_short_words_are_left_alone_by_every_strategy() -> None:
    from rag_app.features import _TYPO_STRATEGIES

    word = "SQL"  # len 3, below every strategy's len >= 4 floor
    for name, strategy in _TYPO_STRATEGIES.items():
        assert strategy(word) == word, name


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


def test_a_route_with_no_tags_at_all_fails_the_check() -> None:
    """DA-13-A-2: before this test, a route added with NO ``tags=`` at all contributed
    nothing to ``used_tags``, so check 1 (unclaimed tag) passed vacuously for it -- a
    forgotten registry entry would never be caught. This proves the independent, dedicated
    check that every ``APIRoute`` declares at least one tag, regardless of whether anything
    claims it."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/totally-new-thing-with-no-tags")
    def _new_route() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(router)
    problems = check_registry(app, metrics.REGISTRY)
    assert any(
        "/totally-new-thing-with-no-tags" in problem and "no tags" in problem
        for problem in problems
    ), problems


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


# --- check_dod_compliance: T13.5.1's DoD rule, enforced for every NEW entry ---------------


def test_check_dod_compliance_has_no_problems_against_the_real_registry() -> None:
    """Every entry added since 13-E follows the DoD rule; every entry that predates it is
    explicitly grandfathered (see ``_DOD_GRANDFATHERED_IDS``'s docstring) -- so the real
    registry, as-is, has no problems today."""
    assert check_dod_compliance() == []


# DEC-039 (DA-13-E-1): the grandfather list may only SHRINK. Without this pin a future change
# could quietly add a new, non-compliant entry's id to it and every DoD test above would stay
# green. Closing a grandfathered entry -> remove its id AND lower this number in the same
# change; raising it is never allowed (a new entry must follow the DoD rule instead).
DOD_GRANDFATHERED_COUNT = 28  # T13.8.2 closed "quality-per-stage-latency" for real


def test_the_dod_grandfather_list_is_frozen_and_only_lists_real_entries() -> None:
    from rag_app.features import _DOD_GRANDFATHERED_IDS

    assert len(_DOD_GRANDFATHERED_IDS) == DOD_GRANDFATHERED_COUNT, (
        "the DoD grandfather list changed size -- it may only shrink (lower"
        " DOD_GRANDFATHERED_COUNT with the id you closed); never add a new entry to it"
    )
    registry_ids = {entry.id for entry in REGISTRY}
    stale = sorted(_DOD_GRANDFATHERED_IDS - registry_ids)
    assert not stale, f"grandfathered ids that are no longer in the registry: {stale}"


def test_a_new_entry_with_no_probe_and_no_metric_fails_the_check() -> None:
    """Done-when (T13.5.1): a brand-new feature that skips the DoD rule entirely (no probe,
    no metric) fails -- proven here with a throwaway entry rather than waiting for someone
    to actually forget."""
    bogus_entry = FeatureEntry(
        id="test-only-dod-bogus-entry",
        name="bogus",
        phase="13",
        status="enabled",
        docs="docs/TRD.md",
    )
    problems = check_dod_compliance(entries=(bogus_entry,))
    assert any("needs a health_probe or a quality_probes" in p for p in problems)
    assert any("needs >= 1 metric" in p for p in problems)


def test_a_new_entry_with_a_probe_and_a_metric_passes_the_check() -> None:
    bogus_entry = FeatureEntry(
        id="test-only-dod-compliant-entry",
        name="compliant",
        phase="13",
        status="enabled",
        docs="docs/TRD.md",
        health_probe=module_probe("rag_app.metrics"),
        metrics=("secrag_chat_stage_seconds",),
    )
    assert check_dod_compliance(entries=(bogus_entry,)) == []


def test_a_grandfathered_entry_is_exempt_even_without_a_probe_or_metric() -> None:
    """``azure-internal-ingress`` has neither a probe nor a metric (by design -- it is not
    reachable at all outside Azure) and predates the DoD rule: still no problems."""
    grandfathered = next(e for e in REGISTRY if e.id == "azure-internal-ingress")
    assert grandfathered.health_probe is None
    assert not grandfathered.quality_probes
    assert not grandfathered.metrics
    assert check_dod_compliance(entries=(grandfathered,)) == []


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
