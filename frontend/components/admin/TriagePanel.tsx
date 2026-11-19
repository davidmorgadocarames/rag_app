"use client";

// T13.8.5: the feature triage list -- every registry feature, worst first, with status
// filters (All/Critical/Warning/Off here/OK) and counts. Replaces the old, unexplained
// "N probe(s) not ok" summary (OverviewPanel) and the plain feature-card grid (FeatureCards)
// with one real list whose rows are buttons (keyboard + aria-pressed) that open the detail
// drawer alongside it (FeatureDrawer, T13.8.6).

import type { FeatureHealthOut } from "@/lib/api";
import {
  countFeaturesByStatus,
  featureGroup,
  featureStatus,
  filterFeaturesByStatus,
  sortFeaturesWorstFirst,
  TRIAGE_FILTERS,
  type FeatureStatusKey,
  type TriageFilterKey,
} from "@/lib/adminTriage";

import StatusBadge from "./StatusBadge";

interface TriagePanelProps {
  features: FeatureHealthOut[];
  filter: TriageFilterKey;
  onFilterChange: (filter: TriageFilterKey) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export default function TriagePanel({
  features,
  filter,
  onFilterChange,
  selectedId,
  onSelect,
}: TriagePanelProps) {
  const sorted = sortFeaturesWorstFirst(features);
  const counts = countFeaturesByStatus(sorted);
  const shown = filterFeaturesByStatus(sorted, filter);

  return (
    <section
      data-testid="admin-triage-panel"
      aria-label="Feature triage"
      className="flex min-w-0 flex-[2] flex-col gap-3 border border-vault-steel-dark p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">FEATURES &amp; PROBES</h2>
        <span className="text-xs text-vault-steel-dark">Worst first &middot; click a row for detail</span>
      </div>

      <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-2">
        {TRIAGE_FILTERS.map((f) => {
          const count = f.key === "all" ? features.length : counts[f.key as FeatureStatusKey];
          const pressed = f.key === filter;
          return (
            <button
              key={f.key}
              type="button"
              data-testid={`triage-filter-${f.key}`}
              aria-pressed={pressed}
              onClick={() => onFilterChange(f.key)}
              className={`cursor-pointer rounded-full border px-3 py-1 text-xs ${
                pressed
                  ? "border-vault-amber text-vault-amber-bright"
                  : "border-vault-steel-dark text-vault-steel hover:border-vault-steel"
              }`}
            >
              {f.label} <span className="font-mono">{count}</span>
            </button>
          );
        })}
      </div>

      <div className="flex flex-col border-t border-vault-steel-dark">
        {shown.map((feature) => {
          const info = featureStatus(feature);
          const selected = feature.id === selectedId;
          return (
            <button
              key={feature.id}
              type="button"
              data-testid={`triage-row-${feature.id}`}
              aria-pressed={selected}
              onClick={() => onSelect(feature.id)}
              className={`flex w-full flex-wrap items-center gap-3 border-b border-vault-steel-dark/40 px-2 py-2.5 text-left ${
                selected ? "bg-vault-amber/10 shadow-[inset_3px_0_0_var(--amber)]" : ""
              }`}
            >
              <StatusBadge tone={info.key} label={info.label} />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="text-vault-steel-light">{feature.name}</span>
                <span className="text-xs text-vault-steel-dark">{info.detail}</span>
              </span>
              <span className="min-w-[4.5rem] text-right font-mono text-xs text-vault-steel-dark">
                {featureGroup(feature.id)}
              </span>
            </button>
          );
        })}
        {shown.length === 0 && (
          <p className="py-4 text-vault-steel-dark">Nothing in this state right now.</p>
        )}
      </div>
    </section>
  );
}
