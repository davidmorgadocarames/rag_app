"use client";

// T13.8.5: the feature triage list -- every registry feature, worst first, with status
// filters (All/Critical/Warning/Off here/OK) and counts. Replaces the old, unexplained
// "N probe(s) not ok" summary (OverviewPanel) and the plain feature-card grid (FeatureCards)
// with one real list whose rows are buttons (keyboard + aria-pressed) that open the detail
// drawer alongside it (FeatureDrawer, T13.8.6).

import { TONE_TEXT_CLASS } from "@/lib/adminColours";
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
      className="flex min-w-0 flex-[2] flex-col gap-3 rounded-admin-card border border-vault-line bg-vault-surface p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm tracking-[0.18em] text-vault-text-strong">FEATURES &amp; PROBES</h2>
        <span className="text-xs text-vault-text-muted">Worst first &middot; click a row for detail</span>
      </div>

      <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-2">
        {TRIAGE_FILTERS.map((f) => {
          const count = f.key === "all" ? features.length : counts[f.key as FeatureStatusKey];
          const pressed = f.key === filter;
          // T13.J.1: the chip's TEXT colour always carries its own tone identity (matches the
          // mockup exactly -- a Critical chip's label is always the crit colour, whether
          // pressed or not); only the border/background toggle to show which chip is active.
          const toneTextClass = f.key === "all" ? "text-vault-text-strong" : TONE_TEXT_CLASS[f.key as FeatureStatusKey];
          return (
            <button
              key={f.key}
              type="button"
              data-testid={`triage-filter-${f.key}`}
              aria-pressed={pressed}
              onClick={() => onFilterChange(f.key)}
              // T13.12.1/T13.12.2: `min-h-9` (36px) + `px-3 py-1.5` (12/6px) match the
              // mockup's own filter-chip style string exactly (Main.dc.html `filters[].style`)
              // -- `bg-vault-amber-wash-14` replaces the pre-13-J3 `bg-vault-amber with a /10 modifier` (an
              // opacity modifier on a var() colour, compiled to no rule at all).
              className={`flex min-h-9 cursor-pointer items-center rounded-full border px-3 py-1.5 text-xs ${toneTextClass} ${
                pressed
                  ? "border-vault-amber bg-vault-amber-wash-14"
                  : "border-vault-steel-dark hover:border-vault-steel"
              }`}
            >
              {f.label} <span className="font-mono">{count}</span>
            </button>
          );
        })}
      </div>

      <div className="flex flex-col border-t border-vault-line">
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
              // T13.12.1/T13.12.2: `min-h-[52px]` + `px-3 py-2.5` (12/10px) match the mockup's
              // own row style string -- `border-vault-row-divider` replaces the pre-13-J3
              // `border-vault-line with a /60 modifier` (same opacity-modifier bug as the amber wash above:
              // --line is also a var() colour, so the divider never rendered at all) and
              // `bg-vault-amber-wash-10` replaces `bg-vault-amber with a /10 modifier`.
              className={`flex min-h-[52px] w-full flex-wrap items-center gap-3 border-b border-vault-row-divider px-3 py-2.5 text-left ${
                selected ? "bg-vault-amber-wash-10 shadow-[inset_3px_0_0_var(--amber)]" : ""
              }`}
            >
              <StatusBadge tone={info.key} label={info.label} />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="font-medium text-vault-text-strong">{feature.name}</span>
                <span className="text-xs text-vault-text-muted">{info.detail}</span>
              </span>
              <span className="min-w-[4.5rem] text-right font-mono text-xs text-vault-text-muted">
                {featureGroup(feature.id)}
              </span>
            </button>
          );
        })}
        {shown.length === 0 && (
          <p className="py-4 text-vault-text-muted">Nothing in this state right now.</p>
        )}
      </div>
    </section>
  );
}
