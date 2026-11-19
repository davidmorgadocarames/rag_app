"""Unit tests for Prometheus metrics (T11.3.2, TF4): separate port, stage-only labels,
in-flight gauge, no PII.
"""

from __future__ import annotations

import time

import httpx
import pytest

from rag_app import metrics


def test_stage_histogram_has_only_a_stage_label() -> None:
    """No-PII-label test: never a user id, IP, question or answer — just ``stage``."""
    assert metrics.STAGE_SECONDS._labelnames == ("stage",)


def test_in_flight_gauge_has_no_labels() -> None:
    assert metrics.CHAT_IN_FLIGHT._labelnames == ()


def test_track_in_flight_increments_and_decrements() -> None:
    before = metrics.CHAT_IN_FLIGHT._value.get()
    with metrics.track_in_flight():
        assert metrics.CHAT_IN_FLIGHT._value.get() == before + 1
    assert metrics.CHAT_IN_FLIGHT._value.get() == before


def test_track_in_flight_decrements_even_on_exception() -> None:
    before = metrics.CHAT_IN_FLIGHT._value.get()
    with pytest.raises(RuntimeError):
        with metrics.track_in_flight():
            raise RuntimeError("boom")
    assert metrics.CHAT_IN_FLIGHT._value.get() == before


def test_metrics_server_serves_observed_stages_on_its_own_port() -> None:
    port = metrics.start_metrics_server(0, addr="127.0.0.1")
    metrics.observe_stage("rerank_inference", 0.123)
    body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    assert 'secrag_chat_stage_seconds_bucket{le="0.25",stage="rerank_inference"}' in body
    assert "secrag_chat_requests_in_flight" in body
    # No PII ever reaches a metric label or help text.
    assert "question" not in body.lower()
    assert "@" not in body


def test_start_metrics_server_is_idempotent() -> None:
    first = metrics.start_metrics_server(0, addr="127.0.0.1")
    second = metrics.start_metrics_server(0, addr="127.0.0.1")
    assert first == second


def test_api_port_never_serves_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API app itself registers no /metrics route — it lives on a separate port only."""
    from fastapi.testclient import TestClient

    from rag_app.api.app import create_app

    client = TestClient(create_app())  # no lifespan: metrics server is irrelevant here
    res = client.get("/metrics")
    assert res.status_code == 404


# --- T13.0.6 (DEC-028 A5, D-ER): no-PII-label tests for the "missing metrics" ------------


def test_rate_limit_hits_total_has_only_a_route_group_label() -> None:
    assert metrics.RATE_LIMIT_HITS_TOTAL._labelnames == ("route_group",)


def test_lockout_hits_total_has_no_labels() -> None:
    assert metrics.LOCKOUT_HITS_TOTAL._labelnames == ()


def test_signup_blocked_total_has_only_a_reason_label() -> None:
    assert metrics.SIGNUP_BLOCKED_TOTAL._labelnames == ("reason",)


def test_csp_reports_total_has_no_labels() -> None:
    assert metrics.CSP_REPORTS_TOTAL._labelnames == ()


def test_mfa_step_up_total_has_only_a_result_label() -> None:
    assert metrics.MFA_STEP_UP_TOTAL._labelnames == ("result",)


def test_erasure_gauges_and_counter_have_no_labels() -> None:
    assert metrics.ERASURE_PENDING._labelnames == ()
    assert metrics.ERASURE_OLDEST_AGE_SECONDS._labelnames == ()
    assert metrics.ERASURE_FAILURES_TOTAL._labelnames == ()


# --- T13.5.1: registry-wide no-PII-label check (catches metric N+1 automatically) --------


def test_check_no_pii_labels_passes_on_the_real_module() -> None:
    """Every metric actually declared in ``rag_app.metrics`` today only uses labels already
    reviewed into ``SAFE_METRIC_LABELS`` -- the per-metric tests above already proved this
    one metric at a time; this proves it once, generically, over the whole module."""
    assert metrics.check_no_pii_labels() == []


def test_check_no_pii_labels_catches_an_unreviewed_label() -> None:
    """Done-when (T13.5.1): a NEW metric with a PII-shaped label fails the check without
    anyone having to write a dedicated per-metric test for it first -- this is exactly the
    gap the per-metric tests above cannot close on their own (they only ever cover a metric
    someone remembered to add a test for)."""
    from prometheus_client import CollectorRegistry, Counter

    throwaway_registry = CollectorRegistry()
    unsafe_counter = Counter(
        "secrag_test_only_total",
        "test only",
        ["user_email"],
        registry=throwaway_registry,
    )
    problems = metrics.check_no_pii_labels({"unsafe_counter": unsafe_counter})
    assert any("user_email" in problem for problem in problems)


def test_check_no_pii_labels_ignores_non_metric_objects() -> None:
    """A plain value with no ``_labelnames`` attribute (most of a module's globals) is
    silently skipped, never a crash."""
    assert metrics.check_no_pii_labels({"not_a_metric": 42, "also_not": "hello"}) == []


def test_new_metrics_are_served_and_carry_no_pii() -> None:
    port = metrics.start_metrics_server(0, addr="127.0.0.1")
    metrics.RATE_LIMIT_HITS_TOTAL.labels(route_group="chat").inc()
    metrics.SIGNUP_BLOCKED_TOTAL.labels(reason="disposable_email").inc()
    metrics.MFA_STEP_UP_TOTAL.labels(result="success").inc()
    metrics.LOCKOUT_HITS_TOTAL.inc()
    metrics.CSP_REPORTS_TOTAL.inc()
    metrics.ERASURE_PENDING.set(1)
    metrics.ERASURE_OLDEST_AGE_SECONDS.set(3600)
    metrics.ERASURE_FAILURES_TOTAL.inc()
    body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    for name in (
        "secrag_rate_limit_hits_total",
        "secrag_lockout_hits_total",
        "secrag_signup_blocked_total",
        "secrag_csp_reports_total",
        "secrag_mfa_step_up_total",
        "secrag_erasure_pending",
        "secrag_erasure_oldest_age_seconds",
        "secrag_erasure_failures_total",
    ):
        assert name in body
    assert "@" not in body
    assert "question" not in body.lower()


# --- T13.2.5: stage_latency_summary / _quantile_from_buckets (latency panel) --------------


def _fresh_stage_histogram() -> tuple[object, object]:
    """A throwaway registry + histogram, same name/buckets as the real STAGE_SECONDS --
    isolated from every other test's calls to ``observe_stage`` in this pytest session."""
    from prometheus_client import CollectorRegistry, Histogram

    registry = CollectorRegistry()
    histogram = Histogram(
        "secrag_chat_stage_seconds",
        "test",
        ["stage"],
        registry=registry,
        buckets=metrics.STAGE_SECONDS._upper_bounds[:-1],  # exclude the implicit +Inf
    )
    return registry, histogram


def test_quantile_from_buckets_interpolates_linearly() -> None:
    # 10 observations spread evenly 0..10 across buckets [0,5), [5,10], +Inf.
    buckets = [(5.0, 5.0), (10.0, 10.0), (float("inf"), 10.0)]
    assert metrics._quantile_from_buckets(buckets, 10.0, 0.5) == pytest.approx(5.0)
    assert metrics._quantile_from_buckets(buckets, 10.0, 0.9) == pytest.approx(9.0)


def test_quantile_from_buckets_returns_none_with_no_observations() -> None:
    assert metrics._quantile_from_buckets([(1.0, 0.0)], 0.0, 0.5) is None


def test_stage_latency_summary_computes_p50_p95_count_avg_per_stage() -> None:
    registry, histogram = _fresh_stage_histogram()
    for value in (0.05, 0.1, 1.5):
        histogram.labels(stage="embed").observe(value)
    histogram.labels(stage="generate").observe(2.0)

    summary = metrics.stage_latency_summary(registry, "secrag_chat_stage_seconds")

    assert summary["embed"]["count"] == 3.0
    assert summary["embed"]["avg_seconds"] == pytest.approx((0.05 + 0.1 + 1.5) / 3)
    assert summary["embed"]["p50_seconds"] is not None
    assert summary["embed"]["p95_seconds"] is not None
    assert summary["embed"]["p50_seconds"] <= summary["embed"]["p95_seconds"]
    assert summary["generate"]["count"] == 1.0


def test_stage_latency_summary_is_empty_with_no_observations() -> None:
    registry, _histogram = _fresh_stage_histogram()
    assert metrics.stage_latency_summary(registry, "secrag_chat_stage_seconds") == {}


# --- T13.8.1: secrag_chat_step_seconds (stage, step), a FIXED reviewed allow-list ----------


def test_step_histogram_has_exactly_the_stage_and_step_labels() -> None:
    assert metrics.STEP_SECONDS._labelnames == ("stage", "step")


def test_step_allowlist_matches_the_approved_pipeline() -> None:
    """Pins the exact (stage, step) vocabulary agreed in the 13-G mockup/HANDOFF -- any
    future change to it is a deliberate, reviewed edit of this test too."""
    assert metrics.STEP_ALLOWLIST == (
        ("classifying", ("classify_intent",)),
        (
            "retrieving",
            ("embed_query", "vector_search", "bm25_search", "rrf_fusion", "load_chunks"),
        ),
        ("reranking", ("wait_slot", "cross_encoder", "order_by_scores")),
        ("generating", ("build_prompt", "llm_first_token", "token_stream", "parse_answer")),
        ("checking", ("build_check_prompt", "llm_verdict", "parse_verdict")),
        ("responding", ("stream_canned_reply",)),
    )


def test_observe_step_records_an_allowed_pair() -> None:
    port = metrics.start_metrics_server(0, addr="127.0.0.1")
    metrics.observe_step("retrieving", "embed_query", 0.042)
    body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    expected = 'secrag_chat_step_seconds_bucket{le="0.05",stage="retrieving",step="embed_query"}'
    assert expected in body
    assert "@" not in body
    assert "question" not in body.lower()


def test_observe_step_rejects_a_pair_outside_the_allowlist() -> None:
    """Done-when (T13.8.1): an unlisted (stage, step) is REJECTED -- it never reaches the
    histogram at all (``STEP_SECONDS`` label values stay exactly the reviewed set)."""
    with pytest.raises(metrics.UnknownPipelineStepError):
        metrics.observe_step("retrieving", "not_a_real_step", 0.01)
    with pytest.raises(metrics.UnknownPipelineStepError):
        metrics.observe_step("not_a_real_stage", "embed_query", 0.01)
    with pytest.raises(metrics.UnknownPipelineStepError):
        # A step that exists, but under a DIFFERENT stage, is still rejected.
        metrics.observe_step("checking", "embed_query", 0.01)


def _fresh_step_histogram() -> tuple[object, object]:
    from prometheus_client import CollectorRegistry, Histogram

    registry = CollectorRegistry()
    histogram = Histogram(
        "secrag_chat_step_seconds",
        "test",
        ["stage", "step"],
        registry=registry,
        buckets=metrics.STEP_SECONDS._upper_bounds[:-1],
    )
    return registry, histogram


def test_step_latency_summary_computes_p50_p95_count_avg_per_pair() -> None:
    registry, histogram = _fresh_step_histogram()
    for value in (0.01, 0.02, 0.03):
        histogram.labels(stage="retrieving", step="embed_query").observe(value)
    histogram.labels(stage="retrieving", step="vector_search").observe(0.5)

    summary = metrics.step_latency_summary(registry, "secrag_chat_step_seconds")

    key = ("retrieving", "embed_query")
    assert summary[key]["count"] == 3.0
    assert summary[key]["avg_seconds"] == pytest.approx((0.01 + 0.02 + 0.03) / 3)
    assert summary[key]["p50_seconds"] is not None
    assert summary[("retrieving", "vector_search")]["count"] == 1.0


def test_step_latency_summary_is_empty_with_no_observations() -> None:
    registry, _histogram = _fresh_step_histogram()
    assert metrics.step_latency_summary(registry, "secrag_chat_step_seconds") == {}


def test_step_sums_approximately_match_stage_time_on_a_fake_pipeline_run() -> None:
    """T13.8.2 done-when: run a fake pipeline through ``rag_app.timing`` with BOTH callbacks
    wired to throwaway registries, and check that the sum of a stage's own step durations is
    close to (never exceeds, barely under) the stage's own total -- the atomic steps really
    do partition the stage's time, they do not double-count or drop most of it."""
    from rag_app import timing

    stage_registry, stage_histogram = _fresh_stage_histogram()
    step_registry, step_histogram = _fresh_step_histogram()

    def on_stage(name: str, elapsed_s: float) -> None:
        stage_histogram.labels(stage=name).observe(elapsed_s)

    def on_step(stage_name: str, step_name: str, elapsed_s: float) -> None:
        step_histogram.labels(stage=stage_name, step=step_name).observe(elapsed_s)

    with timing.recorder(streaming=True, on_stage=on_stage, on_step=on_step):
        with timing.stage("hybrid_search"):
            with timing.step("retrieving", "vector_search"):
                time.sleep(0.01)
            with timing.step("retrieving", "bm25_search"):
                time.sleep(0.01)
            with timing.step("retrieving", "rrf_fusion"):
                time.sleep(0.005)

    stage_summary = metrics.stage_latency_summary(stage_registry, "secrag_chat_stage_seconds")
    step_summary = metrics.step_latency_summary(step_registry, "secrag_chat_step_seconds")

    stage_total = stage_summary["hybrid_search"]["avg_seconds"]
    step_sum = sum(
        step_summary[("retrieving", step)]["avg_seconds"]
        for step in ("vector_search", "bm25_search", "rrf_fusion")
    )
    assert stage_total is not None
    assert step_sum <= stage_total + 0.02  # a little scheduling slack, never a huge gap
    assert step_sum >= stage_total - 0.02
