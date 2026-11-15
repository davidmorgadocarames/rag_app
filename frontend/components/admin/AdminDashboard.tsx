"use client";

// T13.2.1-T13.2.8: the admin dashboard's CSR half. The SSR shell (app/admin/page.tsx)
// already proved role+MFA server-side and did the first `/admin/features` load (T13.2.1);
// everything from here on is client-rendered and kept live either by SSE (T13.4.1, local)
// or low-frequency polling + a manual refresh (T13.4.2, Azure) -- the MODE itself comes
// from the backend's own `live_mode` field (TF14), never a frontend build-time variable.

import { useCallback, useEffect, useRef, useState } from "react";

import {
  adminErasure,
  adminEval,
  adminFeatures,
  adminLatency,
  type AdminErasureResponse,
  type AdminEvalResponse,
  type AdminFeaturesResponse,
  type AdminLatencyResponse,
  type FeatureHealthOut,
} from "@/lib/api";
import { closedReasonMessage, parseAdminStreamData } from "@/lib/adminStream";

import ErasurePanel from "./ErasurePanel";
import LatencyPanel from "./LatencyPanel";
import QualityPanel from "./QualityPanel";
import SecurityPanel from "./SecurityPanel";

// Azure (T13.4.2): low frequency + a manual "Refresh now" button, never a tight loop --
// Container Apps scale-to-zero + internal ingress make this the server-side-decided mode.
const POLL_INTERVAL_MS = 60_000;

interface AdminDashboardProps {
  initial: AdminFeaturesResponse;
}

const STATUS_COLOR: Record<string, string> = {
  enabled: "text-vault-steel-light",
  disabled: "text-vault-steel-dark",
  "retained-not-adopted": "text-vault-steel-dark",
  "expected-fail": "text-vault-amber",
};

function OverviewPanel({ features, liveMode }: { features: FeatureHealthOut[]; liveMode: string }) {
  const byStatus = features.reduce<Record<string, number>>((acc, f) => {
    acc[f.status] = (acc[f.status] ?? 0) + 1;
    return acc;
  }, {});
  const unhealthy = features.filter(
    (f) => f.probe_status !== null && f.probe_status !== "ok",
  ).length;

  return (
    <section
      data-testid="admin-overview-panel"
      className="flex flex-col gap-2 border border-vault-steel-dark p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">OVERVIEW</h2>
      <p className="text-xs text-vault-steel">
        {features.length} registry entries -- {unhealthy} probe(s) not ok -- live updates:{" "}
        <span className="text-vault-amber">{liveMode}</span>
      </p>
      <ul className="flex flex-wrap gap-3 text-xs text-vault-steel-dark">
        {Object.entries(byStatus).map(([status, count]) => (
          <li key={status}>
            {status}: <span className="text-vault-steel-light">{count}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function FeatureCards({ features }: { features: FeatureHealthOut[] }) {
  return (
    <section
      data-testid="admin-feature-cards"
      className="grid grid-cols-1 gap-2 border border-vault-steel-dark p-4 sm:grid-cols-2 lg:grid-cols-3"
    >
      {features.map((f) => (
        <article
          key={f.id}
          data-testid={`feature-card-${f.id}`}
          className="flex flex-col gap-1 border border-vault-steel-dark/60 p-2 text-xs"
        >
          <h3 className="text-vault-steel-light">{f.name}</h3>
          <p className={STATUS_COLOR[f.status] ?? "text-vault-steel"}>{f.status}</p>
          {f.probe_status && (
            <p className="text-vault-steel-dark">
              probe: {f.probe_status} -- {f.probe_detail}
            </p>
          )}
        </article>
      ))}
    </section>
  );
}

export default function AdminDashboard({ initial }: AdminDashboardProps) {
  const [features, setFeatures] = useState<FeatureHealthOut[]>(initial.features);
  const [liveMode] = useState(initial.live_mode);
  const [erasure, setErasure] = useState<AdminErasureResponse | null>(null);
  const [latency, setLatency] = useState<AdminLatencyResponse | null>(null);
  const [evalResult, setEvalResult] = useState<AdminEvalResponse | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  const refreshAll = useCallback(async () => {
    const [f, e, l, ev] = await Promise.all([
      adminFeatures(),
      adminErasure(),
      adminLatency(),
      adminEval(),
    ]);
    setFeatures(f.features);
    setErasure(e);
    setLatency(l);
    setEvalResult(ev);
  }, []);

  const refreshErasure = useCallback(async () => {
    setErasure(await adminErasure());
  }, []);

  // First client-side load (SSR only fetched /admin/features) -- regardless of live mode.
  useEffect(() => {
    adminErasure().then(setErasure).catch(() => undefined);
    adminLatency().then(setLatency).catch(() => undefined);
    adminEval().then(setEvalResult).catch(() => undefined);
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
      if (Array.isArray(parsedFeatures)) setFeatures(parsedFeatures as FeatureHealthOut[]);
      if (parsedErasure && typeof parsedErasure === "object") {
        setErasure(parsedErasure as AdminErasureResponse);
      }
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

  return (
    <main className="mx-auto flex max-w-5xl flex-col gap-4 px-6 py-8">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">
          Admin dashboard
        </h1>
        {liveMode === "polling" && (
          <button
            type="button"
            onClick={() => refreshAll()}
            className="cursor-pointer border border-vault-steel-dark px-3 py-1 text-xs tracking-[0.1em] text-vault-steel hover:border-vault-steel"
          >
            Refresh now
          </button>
        )}
      </div>

      {banner && (
        <p data-testid="admin-stream-banner" className="border border-vault-danger p-2 text-xs text-vault-danger">
          {banner}
        </p>
      )}

      <OverviewPanel features={features} liveMode={liveMode} />
      <FeatureCards features={features} />
      <LatencyPanel latency={latency} />
      <SecurityPanel features={features} />
      <QualityPanel evalResult={evalResult} features={features} />
      <ErasurePanel erasure={erasure} onChanged={refreshErasure} />
    </main>
  );
}
