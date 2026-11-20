"use client";

// T13.8.6/T13.8.8: the side drawer for the feature selected in Triage -- name/id/phase, its
// probe status badge, the registry's own lifecycle status (DA-13-G2-3), the probe detail (+
// the "why" when it is Off here by design), its own metrics map, related alerts (via
// AdminAlertOut.feature_ids, T13.8.3a) and the registry's docs link (T13.8.6/backend docs
// field).

import { TONE_TEXT_CLASS } from "@/lib/adminColours";
import type { AdminAlertOut, FeatureHealthOut } from "@/lib/api";
import { featureGroup, featureStatus } from "@/lib/adminTriage";

import StatusBadge from "./StatusBadge";

interface FeatureDrawerProps {
  feature: FeatureHealthOut;
  relatedAlerts: AdminAlertOut[];
}

export default function FeatureDrawer({ feature, relatedAlerts }: FeatureDrawerProps) {
  const info = featureStatus(feature);
  const metricEntries = Object.entries(feature.metrics);

  return (
    <aside
      data-testid="admin-feature-drawer"
      aria-label="Feature detail"
      className="flex min-w-0 flex-1 flex-col gap-4 rounded-lg border border-vault-steel-dark bg-vault-surface-raised p-4 lg:max-w-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-xs uppercase tracking-[0.08em] text-vault-text-muted">
            {featureGroup(feature.id)} &middot; phase {feature.phase}
          </span>
          <h3 className="text-lg font-semibold text-vault-text-strong">{feature.name}</h3>
          <code
            data-testid="admin-feature-drawer-id"
            className="break-all font-mono text-xs text-vault-text-muted"
          >
            {feature.id}
          </code>
        </div>
        <StatusBadge tone={info.key} label={info.label} testId="admin-feature-drawer-status" />
      </div>

      <p
        data-testid="admin-feature-drawer-lifecycle-status"
        className="font-mono text-xs text-vault-text-muted"
      >
        Registry status: <span className="text-vault-steel-light">{feature.status}</span>
      </p>

      <div className="flex flex-col gap-1.5">
        <h4 className="text-xs uppercase tracking-[0.08em] text-vault-text-muted">Health probe</h4>
        <p data-testid="admin-feature-drawer-detail" className="text-vault-text-strong">
          {info.detail}
        </p>
        {info.why && (
          <p
            data-testid="admin-feature-drawer-why"
            className="rounded border border-vault-line bg-vault-off-bg p-2.5 text-xs text-vault-steel-light"
          >
            {info.why}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <h4 className="text-xs uppercase tracking-[0.08em] text-vault-text-muted">
          Metrics (this replica)
        </h4>
        {metricEntries.length === 0 ? (
          <p className="text-xs text-vault-text-muted">No metrics registered for this feature.</p>
        ) : (
          metricEntries.map(([key, value]) => (
            <div
              key={key}
              className="flex justify-between gap-3 border-b border-vault-line/60 py-1 font-mono text-xs"
            >
              <span className="break-all text-vault-text-muted">{key}</span>
              <span className="text-vault-text-strong">{value ?? "n/a"}</span>
            </div>
          ))
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <h4 className="text-xs uppercase tracking-[0.08em] text-vault-text-muted">
          Related alerts
        </h4>
        {relatedAlerts.length === 0 ? (
          <p className="text-xs text-vault-text-muted">None.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {relatedAlerts.map((alert) => (
              <li
                key={alert.code}
                className={`text-xs ${TONE_TEXT_CLASS[alert.severity === "red" ? "crit" : "warn"]}`}
              >
                {alert.message}
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="font-mono text-xs text-vault-text-muted">Docs: {feature.docs}</p>
    </aside>
  );
}
