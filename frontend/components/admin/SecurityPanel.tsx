"use client";

// T13.2.6: security panel -- rate-limit hits, lockouts, CSP reports and signup blocks
// (T13.0.6 metrics, D-ER). Reads the SAME per-feature metrics summary the feature cards
// already carry (AdminFeaturesResponse.features[].metrics) rather than a second fetch:
// MetricsBackend.current_value sums across every label combination a metric carries (same
// semantics as the local backend's own `sum(metric_name)` PromQL query), so a single number
// is everything either backend can offer here -- a per-label breakdown (e.g. rate-limit
// hits BY route group) would need a different query shape, out of this block's scope.

import type { FeatureHealthOut } from "@/lib/api";

interface SecurityPanelProps {
  features: FeatureHealthOut[];
}

const SECURITY_METRICS: { name: string; label: string }[] = [
  { name: "secrag_rate_limit_hits_total", label: "Rate-limit hits" },
  { name: "secrag_lockout_hits_total", label: "Login lockouts" },
  { name: "secrag_csp_reports_total", label: "CSP violation reports" },
  { name: "secrag_signup_blocked_total", label: "Signup blocks" },
  { name: "secrag_mfa_step_up_total", label: "MFA step-up attempts" },
];

function mergedMetrics(features: FeatureHealthOut[]): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const feature of features) Object.assign(out, feature.metrics);
  return out;
}

export default function SecurityPanel({ features }: SecurityPanelProps) {
  const values = mergedMetrics(features);

  return (
    <section
      data-testid="admin-security-panel"
      className="flex flex-col gap-3 border border-vault-steel-dark p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">SECURITY</h2>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {SECURITY_METRICS.map(({ name, label }) => (
          <div
            key={name}
            data-testid={`security-metric-${name}`}
            className="flex flex-col gap-1 border border-vault-steel-dark/60 p-2"
          >
            <span className="text-[10px] tracking-[0.08em] text-vault-steel-dark">{label}</span>
            <span className="text-lg text-vault-amber">{values[name] ?? "n/a"}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
