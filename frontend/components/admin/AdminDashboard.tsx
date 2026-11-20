"use client";

// T13.2.1-T13.2.8/T13.8.4-T13.8.6: the admin dashboard's CSR half. The SSR shell
// (app/admin/page.tsx) already proved role+MFA server-side and did the first
// `/admin/features` load (T13.2.1); everything from here on is client-rendered and kept
// live either by SSE (T13.4.1, local) or low-frequency polling + a manual refresh (T13.4.2,
// Azure) -- the MODE itself comes from the backend's own `live_mode` field (TF14), never a
// frontend build-time variable.
//
// 13-G2 redesign: a header + alert banner + up to four KPI tiles + a left-hand section menu
// (Triage/Pipeline/Quality/Erasure/Defence lab) replace the old always-on card stack. Triage
// (T13.8.5/T13.8.6) is the only section redesigned this block; Pipeline/Quality/Erasure/
// Defence lab render their EXISTING panels unchanged inside the new shell (13-G3 redesigns
// those). Deliberately no "MFA verified" badge here (reviewed, approved mockup change): every
// `/admin/*` request already re-proves the step-up server-side, so a client-side badge would
// just echo a point-in-time fact the UI cannot itself keep guaranteeing mid-session.

import { useCallback, useEffect, useRef, useState } from "react";

import {
  adminErasure,
  adminEval,
  adminFeatures,
  adminLatency,
  type AdminAlertOut,
  type AdminErasureResponse,
  type AdminEvalResponse,
  type AdminFeaturesResponse,
  type AdminLatencyResponse,
  type FeatureHealthOut,
} from "@/lib/api";
import {
  alertSeverityCounts,
  chatLatencySummary,
  erasureQueueSummary,
  featuresHealthySummary,
} from "@/lib/adminKpi";
import { closedReasonMessage, parseAdminStreamData, sortAlertsBySeverity } from "@/lib/adminStream";
import { resolveAlertJump, sortFeaturesWorstFirst, type TriageFilterKey } from "@/lib/adminTriage";
import { useWakeState } from "@/lib/useWakeState";
import { WakeUpError } from "@/lib/wakeUp";

import WakeBanner from "../WakeBanner";
import DefenceLabPanel from "./DefenceLabPanel";
import ErasurePanel from "./ErasurePanel";
import FeatureDrawer from "./FeatureDrawer";
import LatencyPanel from "./LatencyPanel";
import QualityPanel from "./QualityPanel";
import StatusBadge from "./StatusBadge";
import TriagePanel from "./TriagePanel";

// Azure (T13.4.2): low frequency + a manual "Refresh now" button, never a tight loop --
// Container Apps scale-to-zero + internal ingress make this the server-side-decided mode.
const POLL_INTERVAL_MS = 60_000;

interface AdminDashboardProps {
  initial: AdminFeaturesResponse;
}

type Section = "triage" | "pipeline" | "quality" | "erasure" | "lab";

const SECTION_DEFS: { key: Section; label: string }[] = [
  { key: "triage", label: "Triage" },
  { key: "pipeline", label: "Pipeline" },
  { key: "quality", label: "Quality" },
  { key: "erasure", label: "Erasure" },
  { key: "lab", label: "Defence lab" },
];

// T13.8.4/T13.8.7: a severity badge (icon + text, never colour alone) plus a jump action to
// the related feature (AdminAlertOut.feature_ids, T13.8.3a) -- or, for the two erasure-shaped
// alert codes (DEC-043/DA-13-G2-1), always to the Erasure section regardless of feature_ids
// (see adminTriage.ts::resolveAlertJump for why `feature_ids` alone is not enough here).
function AlertBanner({
  alerts,
  features,
  onJumpToFeature,
  onJumpToSection,
}: {
  alerts: AdminAlertOut[];
  features: FeatureHealthOut[];
  onJumpToFeature: (id: string) => void;
  onJumpToSection: (section: Section) => void;
}) {
  if (alerts.length === 0) return null;
  return (
    <section
      aria-label="Active alerts"
      data-testid="admin-alert-banner"
      className="flex flex-col gap-2"
    >
      {sortAlertsBySeverity(alerts).map((alert) => {
        const tone = alert.severity === "red" ? "crit" : "warn";
        const target = resolveAlertJump(alert, features);
        return (
          <div
            key={alert.code}
            data-testid={`admin-alert-${alert.code}`}
            className={`flex flex-wrap items-center gap-3 border p-2.5 text-xs ${
              tone === "crit" ? "border-vault-danger" : "border-vault-amber"
            }`}
          >
            <StatusBadge tone={tone} label={alert.severity === "red" ? "Critical" : "Warning"} />
            <span className="flex-1 text-vault-steel-light">{alert.message}</span>
            {target.featureId ? (
              <button
                type="button"
                data-testid={`admin-alert-${alert.code}-open`}
                onClick={() => onJumpToFeature(target.featureId as string)}
                className="cursor-pointer border border-vault-steel-dark px-2.5 py-1 text-vault-amber hover:border-vault-steel"
              >
                Open feature
              </button>
            ) : target.section ? (
              <button
                type="button"
                data-testid={`admin-alert-${alert.code}-open`}
                onClick={() => onJumpToSection(target.section as Section)}
                className="cursor-pointer border border-vault-steel-dark px-2.5 py-1 text-vault-amber hover:border-vault-steel"
              >
                Open erasure
              </button>
            ) : null}
          </div>
        );
      })}
    </section>
  );
}

function KpiTile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="flex flex-col gap-1 border border-vault-steel-dark p-3">
      <span className="text-xs uppercase tracking-[0.08em] text-vault-steel-dark">{label}</span>
      <span className="font-mono text-2xl font-semibold text-vault-steel-light">{value}</span>
      <span className="text-xs text-vault-steel">{sub}</span>
    </div>
  );
}

// T13.8.4: at most four KPI tiles, every number a direct read of real `/admin/*` data (never
// a client-side threshold of its own) -- same "aggregate, this replica only" caveat as the
// rest of the dashboard.
function KpiTiles({
  features,
  alerts,
  latency,
  erasure,
}: {
  features: FeatureHealthOut[];
  alerts: AdminAlertOut[];
  latency: AdminLatencyResponse | null;
  erasure: AdminErasureResponse | null;
}) {
  const health = featuresHealthySummary(features);
  const severity = alertSeverityCounts(alerts);
  const chat = chatLatencySummary(latency?.stages ?? []);
  const queue = erasureQueueSummary(erasure?.requests ?? []);

  return (
    <section
      aria-label="Key indicators"
      data-testid="admin-kpi-tiles"
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
    >
      <KpiTile
        label="Features healthy"
        value={`${health.healthy} / ${health.countedTotal}`}
        sub={`${health.critical} critical · ${health.warning} warning · ${health.offByDesign} off by design`}
      />
      <KpiTile
        label="Active alerts"
        value={String(alerts.length)}
        sub={`${severity.red} red · ${severity.amber} amber`}
      />
      <KpiTile
        label="Chat latency (avg)"
        value={chat.avgSeconds !== null ? `${chat.avgSeconds.toFixed(2)}s` : "n/a"}
        sub={chat.slowestStage ? `slowest stage: ${chat.slowestStage}` : "no data yet"}
      />
      <KpiTile
        label="Erasure queue"
        value={String(queue.pending)}
        sub={`${queue.overdue} overdue`}
      />
    </section>
  );
}

export default function AdminDashboard({ initial }: AdminDashboardProps) {
  const [features, setFeatures] = useState<FeatureHealthOut[]>(initial.features);
  const [liveMode] = useState(initial.live_mode);
  const [erasure, setErasure] = useState<AdminErasureResponse | null>(null);
  const [latency, setLatency] = useState<AdminLatencyResponse | null>(null);
  const [evalResult, setEvalResult] = useState<AdminEvalResponse | null>(null);
  const [alerts, setAlerts] = useState<AdminAlertOut[]>(initial.alerts ?? []);
  const [banner, setBanner] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  const [section, setSection] = useState<Section>("triage");
  const [triageFilter, setTriageFilter] = useState<TriageFilterKey>("all");
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(
    () => sortFeaturesWorstFirst(initial.features)[0]?.id ?? null,
  );
  // T13.8.4: "refreshed Ns ago" -- a plain wall-clock tick, display-only, never influences
  // when data is actually re-fetched (that stays SSE/poll-interval driven below).
  const [lastSync, setLastSync] = useState<number>(() => Date.now());
  const [now, setNow] = useState<number>(() => Date.now());
  // T13.9.1 (DEBT-056): every /admin/* call here is a GET -- idempotent by construction, so
  // lib/wakeUp.ts always retries a cold-start-shaped failure automatically, whatever its
  // "safe"/"unsafe" classification; this hook only drives the shared banner around that
  // already-safe retry loop. If the retry budget is exhausted, "Refresh now" below retries
  // explicitly on the user's own click (polling will also pick it back up on its own tick).
  const wake = useWakeState();

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // wake's onWaking/onRecovered/onFailed/reset are stable (useCallback, no deps) -- used
  // directly rather than the `wake` object itself so this callback's own identity does not
  // change every time the waking banner toggles (which would otherwise restart the poll
  // interval below on every wake/recover).
  const { onWaking, onRecovered, onFailed, reset: resetWake } = wake;
  const refreshAll = useCallback(async () => {
    try {
      // DA-13-E-2: alerts come with /admin/features (same probe sweep) -- no separate poll.
      const [f, e, l, ev] = await Promise.all([
        adminFeatures({ onWaking, onRecovered }),
        adminErasure({ onWaking, onRecovered }),
        adminLatency({ onWaking, onRecovered }),
        adminEval({ onWaking, onRecovered }),
      ]);
      setFeatures(f.features);
      setErasure(e);
      setLatency(l);
      setEvalResult(ev);
      setAlerts(f.alerts ?? []);
      setLastSync(Date.now());
      resetWake();
    } catch (err) {
      if (err instanceof WakeUpError) {
        onFailed();
        return;
      }
      throw err;
    }
  }, [onWaking, onRecovered, onFailed, resetWake]);

  const refreshErasure = useCallback(async () => {
    setErasure(await adminErasure());
    setLastSync(Date.now());
  }, []);

  // First client-side load (SSR only fetched /admin/features) -- regardless of live mode.
  useEffect(() => {
    adminErasure()
      .then((e) => {
        setErasure(e);
        setLastSync(Date.now());
      })
      .catch(() => undefined);
    adminLatency()
      .then((l) => {
        setLatency(l);
        setLastSync(Date.now());
      })
      .catch(() => undefined);
    adminEval()
      .then((ev) => {
        setEvalResult(ev);
        setLastSync(Date.now());
      })
      .catch(() => undefined);
  }, []);

  // T13.4.1: SSE, local only.
  useEffect(() => {
    if (liveMode !== "sse") return undefined;
    const source = new EventSource("/api/admin/stream", { withCredentials: true });
    eventSourceRef.current = source;

    function applySnapshot(event: MessageEvent) {
      const data = parseAdminStreamData(event.data);
      if (!data) return;
      const parsedFeatures = data.features;
      const parsedErasure = data.erasure;
      const parsedAlerts = data.alerts;
      if (Array.isArray(parsedFeatures)) setFeatures(parsedFeatures as FeatureHealthOut[]);
      if (parsedErasure && typeof parsedErasure === "object") {
        setErasure(parsedErasure as AdminErasureResponse);
      }
      if (Array.isArray(parsedAlerts)) setAlerts(parsedAlerts as AdminAlertOut[]);
      setLastSync(Date.now());
    }

    source.addEventListener("snapshot", applySnapshot);
    source.addEventListener("heartbeat", applySnapshot);
    source.addEventListener("closed", (event) => {
      const data = parseAdminStreamData((event as MessageEvent).data);
      const reason = typeof data?.reason === "string" ? data.reason : "unknown";
      setBanner(closedReasonMessage(reason));
      source.close();
    });

    return () => {
      source.close();
      eventSourceRef.current = null;
    };
  }, [liveMode]);

  // T13.4.2: polling, Azure -- low frequency + a manual "Refresh now" button.
  useEffect(() => {
    if (liveMode !== "polling") return undefined;
    const id = setInterval(() => {
      refreshAll().catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [liveMode, refreshAll]);

  const jumpToFeature = useCallback((featureId: string) => {
    setSection("triage");
    setTriageFilter("all");
    setSelectedFeatureId(featureId);
  }, []);
  const jumpToSection = useCallback((target: Section) => setSection(target), []);

  const sortedFeatures = sortFeaturesWorstFirst(features);
  const selectedFeature =
    features.find((f) => f.id === selectedFeatureId) ?? sortedFeatures[0] ?? null;

  const labFeature = features.find((f) => f.id === "admin-defence-lab");
  // T13.3.1 precedent: same gate DefenceLabPanel itself already uses -- the lab tab only
  // exists when the lab's own routes are actually mounted here (local), never on Azure.
  const labAvailable = labFeature?.probe_status === "ok";

  const health = featuresHealthySummary(features);
  const queueSummary = erasureQueueSummary(erasure?.requests ?? []);
  const navTag: Record<Section, string> = {
    triage: `${health.critical + health.warning} to look at`,
    pipeline: `${latency?.stages.length ?? 0} stages`,
    quality: evalResult ? (evalResult.passed ? "passed" : "regressed") : "no data",
    erasure: `${queueSummary.pending} pending`,
    lab: "local",
  };

  const elapsedSeconds = Math.max(0, Math.round((now - lastSync) / 1000));
  const refreshText =
    liveMode === "polling"
      ? `refreshed ${elapsedSeconds}s ago · polling every ${POLL_INTERVAL_MS / 1000}s`
      : `refreshed ${elapsedSeconds}s ago · live stream`;

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6 sm:px-6 sm:py-8">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-vault-steel-dark pb-4">
        <div>
          <h1
            data-testid="admin-header-title"
            className="font-mono text-xl font-semibold tracking-[0.12em] text-vault-steel-light"
          >
            SECRAG &middot; ADMIN
          </h1>
          <p className="text-xs text-vault-steel-dark">Operations dashboard</p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs text-vault-steel-dark">
          <span className="rounded-full border border-vault-steel-dark px-2.5 py-1 font-mono">
            live mode: <span className="text-vault-steel-light">{liveMode}</span>
          </span>
          <span data-testid="admin-refresh-text" className="font-mono">
            {refreshText}
          </span>
          {liveMode === "polling" && (
            <button
              type="button"
              onClick={() => refreshAll()}
              className="cursor-pointer border border-vault-steel-dark px-3 py-1 tracking-[0.1em] text-vault-steel hover:border-vault-steel"
            >
              Refresh now
            </button>
          )}
        </div>
      </header>

      <WakeBanner state={wake.state} onRetry={() => void refreshAll()} />

      {/* T13.4.3/R6-4 (DA-13-C-2): no managed Prometheus on Azure -- every metric value on
          this dashboard is "now" only, never a time series. Shown whenever the backend's own
          live_mode says "polling" (the Azure indicator, TF14) -- never a frontend build-time
          check. Relocated here from the old standalone OverviewPanel, 13-G2. */}
      {liveMode === "polling" && (
        <p data-testid="admin-history-note" className="text-xs text-vault-steel-dark">
          History: <span className="text-vault-steel-light">Azure: n/a</span> -- every metric
          value below is this replica&apos;s current state only, never a cross-replica time
          series.
        </p>
      )}

      {banner && (
        <p
          data-testid="admin-stream-banner"
          className="border border-vault-danger p-2 text-xs text-vault-danger-text"
        >
          {banner}
        </p>
      )}

      <AlertBanner
        alerts={alerts}
        features={features}
        onJumpToFeature={jumpToFeature}
        onJumpToSection={jumpToSection}
      />

      <KpiTiles features={features} alerts={alerts} latency={latency} erasure={erasure} />

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <nav
          aria-label="Dashboard sections"
          className="flex w-full flex-col gap-1 border border-vault-steel-dark p-2 lg:w-56 lg:flex-shrink-0"
        >
          {SECTION_DEFS.filter((s) => s.key !== "lab" || labAvailable).map((s) => (
            <button
              key={s.key}
              type="button"
              data-testid={`admin-nav-${s.key}`}
              aria-current={section === s.key ? "page" : undefined}
              onClick={() => setSection(s.key)}
              className={`flex items-center justify-between gap-2 rounded px-3 py-2 text-left text-sm ${
                section === s.key
                  ? "bg-vault-amber/10 font-semibold text-vault-amber-bright"
                  : "text-vault-steel hover:text-vault-steel-light"
              }`}
            >
              <span>{s.label}</span>
              <span className="font-mono text-xs text-vault-steel-dark">{navTag[s.key]}</span>
            </button>
          ))}
          {!labAvailable && (
            <p className="px-3 py-2 text-xs text-vault-steel-dark">
              Defence lab appears only on a local stack; it is hidden on Azure.
            </p>
          )}
        </nav>

        <div className="flex min-w-0 flex-1 flex-col gap-4">
          {section === "triage" && (
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
              <TriagePanel
                features={features}
                filter={triageFilter}
                onFilterChange={setTriageFilter}
                selectedId={selectedFeature?.id ?? null}
                onSelect={setSelectedFeatureId}
              />
              {selectedFeature && (
                <FeatureDrawer
                  feature={selectedFeature}
                  relatedAlerts={alerts.filter((a) =>
                    (a.feature_ids ?? []).includes(selectedFeature.id),
                  )}
                />
              )}
            </div>
          )}
          {section === "pipeline" && <LatencyPanel latency={latency} />}
          {section === "quality" && <QualityPanel evalResult={evalResult} features={features} />}
          {section === "erasure" && (
            <ErasurePanel erasure={erasure} onChanged={refreshErasure} />
          )}
          {section === "lab" && <DefenceLabPanel features={features} />}
        </div>
      </div>
    </main>
  );
}
