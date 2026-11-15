"use client";

// T13.2.5: per-stage p50/p95 (P13-D1, Recharts) -- this replica's own in-process histogram
// since it last started, no cross-replica history (same R6-4 caveat as every Azure metric).

import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { AdminLatencyResponse } from "@/lib/api";

interface LatencyPanelProps {
  latency: AdminLatencyResponse | null;
}

export default function LatencyPanel({ latency }: LatencyPanelProps) {
  const stages = latency?.stages ?? [];
  const data = stages.map((s) => ({
    stage: s.stage,
    p50: s.p50_seconds ?? 0,
    p95: s.p95_seconds ?? 0,
  }));

  return (
    <section
      data-testid="admin-latency-panel"
      className="flex flex-col gap-3 border border-vault-steel-dark p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">LATENCY (p50 / p95)</h2>
      {data.length === 0 ? (
        <p className="text-xs text-vault-steel-dark">No stage timings observed yet.</p>
      ) : (
        <div style={{ width: "100%", height: 240 }}>
          <ResponsiveContainer>
            <BarChart data={data}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--steel-dark)" />
              <XAxis dataKey="stage" tick={{ fill: "var(--steel)", fontSize: 11 }} />
              <YAxis
                tick={{ fill: "var(--steel)", fontSize: 11 }}
                label={{ value: "seconds", angle: -90, position: "insideLeft", fill: "var(--steel)" }}
              />
              <Tooltip
                contentStyle={{ background: "var(--plate)", border: "1px solid var(--steel-dark)" }}
              />
              <Legend />
              <Bar dataKey="p50" name="p50" fill="var(--steel-light)" />
              <Bar dataKey="p95" name="p95" fill="var(--amber)" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
      <ul className="grid grid-cols-2 gap-1 text-xs text-vault-steel sm:grid-cols-4">
        {stages.map((s) => (
          <li key={s.stage} data-testid={`latency-stage-${s.stage}`}>
            {s.stage}: p50 {s.p50_seconds?.toFixed(3) ?? "n/a"}s / p95{" "}
            {s.p95_seconds?.toFixed(3) ?? "n/a"}s ({s.count} samples)
          </li>
        ))}
      </ul>
    </section>
  );
}
