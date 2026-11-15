"""``MetricsBackend`` (T13.1.3, P13-D3 follow-on): the admin dashboard reads CURRENT metric
values through this interface, never Prometheus directly -- the Prometheus container is
never exposed through the BFF (PHASE_PLANNING 13.1). Two implementations are planned across
Phase 13:

* **local** (this module, T13.1.3): queries the compose ``prometheus`` container over its
  HTTP API (instant PromQL query), so the admin API process never needs its own in-process
  counters duplicated for the dashboard.
* **Azure** (13-C, T13.4.3, R6-4): there is no managed Prometheus on Azure at all -- that
  implementation reads the REPLICA's own in-process registry instead (current values only;
  history shown as "Azure: n/a"). It must return the SAME shape as this one (contract test).

Every method degrades to ``None`` on any error (unreachable server, bad response, unknown
metric) -- never raises, same discipline as every probe in ``rag_app.features``: a metrics
backend that is down must never turn an admin page into a 500.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

DEFAULT_TIMEOUT_SECONDS = 2.0


class MetricsBackend(Protocol):
    def current_value(self, metric_name: str) -> float | None:
        """The current value of an unlabelled counter/gauge, or ``None`` if unknown/
        unreachable. Never raises."""
        ...

    def current_values(self, metric_names: Sequence[str]) -> dict[str, float | None]:
        """``current_value`` for every name in ``metric_names``, as a dict (never raises)."""
        ...


@dataclass(frozen=True)
class PrometheusMetricsBackend:
    """Local implementation (T13.1.3): one instant PromQL query per metric against
    ``base_url`` (the compose ``prometheus`` service, e.g. ``http://prometheus:9090``).
    ``sum(metric_name)`` collapses any label dimension the metric might carry (every metric
    the registry declares today has none, labelled histograms are read through the latency
    panel's own query, not this generic path) and is harmless (a no-op sum) on an unlabelled
    one. ``base_url=""`` (Azure, or local dev before the stack is up) short-circuits to
    "unknown" for every metric WITHOUT attempting a network call."""

    base_url: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def current_value(self, metric_name: str) -> float | None:
        if not self.base_url:
            return None
        try:
            response = httpx.get(
                f"{self.base_url.rstrip('/')}/api/v1/query",
                params={"query": f"sum({metric_name})"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - unreachable/malformed: report unknown, never raise
            return None
        result = payload.get("data", {}).get("result") or []
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def current_values(self, metric_names: Sequence[str]) -> dict[str, float | None]:
        return {name: self.current_value(name) for name in metric_names}


@dataclass(frozen=True)
class InProcessMetricsBackend:
    """Azure implementation (T13.4.3, R6-4): there is no managed Prometheus at all on Azure,
    so this reads the CURRENT value straight out of THIS process's own ``prometheus_client``
    registry (``rag_app.metrics.REGISTRY``) -- the exact numbers this replica would otherwise
    expose on its internal ``/metrics`` port, with no network call and no sidecar. History is
    never available this way (only ever "now"); the dashboard shows "Azure: n/a" for it
    (frontend concern, not this class's).

    Summed across every label combination a metric carries, matching the local
    ``PrometheusMetricsBackend``'s own ``sum(metric_name)`` query -- so both implementations
    return the SAME shape for the SAME input (contract test:
    ``test_metrics_backend.py::test_both_backends_return_the_same_shape_for_the_same_names``).
    An unknown metric name -- never registered at all -- is ``None`` (unknown), never ``0.0``;
    a registered-but-never-observed metric still has samples (e.g. a Counter at 0 / a Gauge
    at its default), so it reads ``0.0``, same as it would through a real PromQL query."""

    # None -> rag_app.metrics.REGISTRY (this process's own registry, see __post_init__);
    # tests pass a throwaway CollectorRegistry instead so they never touch the real one.
    registry: CollectorRegistry | None = field(default=None)

    def __post_init__(self) -> None:
        if self.registry is None:
            from rag_app import metrics as _metrics_module

            object.__setattr__(self, "registry", _metrics_module.REGISTRY)

    def current_value(self, metric_name: str) -> float | None:
        assert self.registry is not None  # set by __post_init__, always non-None by here
        found = False
        total = 0.0
        for metric in self.registry.collect():
            for sample in metric.samples:
                if sample.name == metric_name:
                    found = True
                    total += sample.value
        return total if found else None

    def current_values(self, metric_names: Sequence[str]) -> dict[str, float | None]:
        return {name: self.current_value(name) for name in metric_names}


def get_metrics_backend() -> MetricsBackend:
    """FastAPI dependency factory: a FRESH backend built from the current settings on every
    call (no module-level caching) -- construction is cheap (one dataclass, no connection
    pool) and this keeps a test's ``monkeypatch.setenv("PROMETHEUS_URL", ...)`` effective
    immediately, with nothing stale to reset between tests.

    ``PROMETHEUS_URL`` set (local, the compose ``prometheus`` service) -> query it over HTTP;
    unset (Azure -- R6-4, no managed Prometheus -- or local dev before the stack is up) ->
    read this process's own registry instead (T13.4.3) rather than degrading straight to
    "unknown": on Azure that is the only metrics source there will ever be, and locally it is
    a strictly more informative fallback than reporting every metric as unreachable."""
    from rag_app.config import get_settings

    url = get_settings().prometheus_url
    if url:
        return PrometheusMetricsBackend(url)
    return InProcessMetricsBackend()
