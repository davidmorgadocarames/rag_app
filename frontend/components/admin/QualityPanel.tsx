"use client";

// T13.2.7/T13.8.8 (13-G3): quality panel -- the last eval run vs. baseline and floors
// (T13.1.4), plus the misspelling probe's own expected-fail status (Phase 14 gap, tracked
// honestly rather than hidden) read from the feature card's health probe.
//
// T13.8.8: `last_run_at === null` (the gate's eval step has never run on this replica) now
// shows an honest "No evaluation recorded yet" empty state instead of the old "Last run:
// never -- passed" -- the backend's own `admin_eval()` defaults `passed=True` when nothing
// has been observed (every metric value is None, so nothing trips below_floor/regressed), so
// the OLD text was not merely unhelpful but actively misleading ("passed" implies a real run).
//
// T13.12-fix (13-J3, user 2026-11-22): restyled to match `Main.dc.html`'s own quality section
// exactly (lines 213-255) -- a header-level tone pill (was missing: only the per-row State
// pill existed before), the table's own header/cell padding + divider colours, and the dashed
// empty-state box -- real data/behaviour unchanged, presentation only.

import { TONE_CLASS } from "@/lib/adminColours";
import type { AdminEvalResponse, FeatureHealthOut } from "@/lib/api";

import StatusBadge from "./StatusBadge";

interface QualityPanelProps {
  evalResult: AdminEvalResponse | null;
  features: FeatureHealthOut[];
}

// T13.J.1: same "ok"/"warn" tones the mockup's own qmetrics table uses (Main.dc.html:
// `q[4]` is "ok" or "warn", never "crit" -- regressed/below-floor on the golden set is a
// warning, not a critical failure) -- tinted pill, icon-free here (the row's own Metric/
// Value text already carries the label, so this badge is the colour signal; "Above floor"/
// "Below baseline" is itself the text, never colour alone).
function StateBadge({ ok }: { ok: boolean }) {
  const tone = ok ? "ok" : "warn";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-semibold ${TONE_CLASS[tone]}`}
    >
      {ok ? "Above floor" : "Below baseline"}
    </span>
  );
}

// T13.12.2 (13-J3): the mockup's own `<th>` style (Main.dc.html line 227-232) -- 12px
// uppercase, 0.06em tracking, muted, 500 weight, 8px/6px padding, a `--line` bottom border.
function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th
      scope="col"
      className={`border-b border-vault-line px-1.5 py-2 text-xs font-medium uppercase tracking-[0.06em] text-vault-text-muted ${
        right ? "text-right" : "text-left"
      }`}
    >
      {children}
    </th>
  );
}

export default function QualityPanel({ evalResult, features }: QualityPanelProps) {
  const misspelling = features.find((f) => f.id === "quality-misspelling-probe");
  const hasRun = evalResult !== null && evalResult.last_run_at !== null;

  return (
    <section
      data-testid="admin-quality-panel"
      className="flex flex-col gap-3.5 rounded-admin-card border border-vault-line bg-vault-surface p-[18px]"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm tracking-[0.18em] text-vault-text-strong">
          ANSWER QUALITY &middot; GOLDEN SET
        </h2>
        {hasRun && evalResult && (
          <StatusBadge
            tone={evalResult.passed ? "ok" : "warn"}
            label={evalResult.passed ? "Passed" : "Regressed"}
            testId="quality-header-badge"
          />
        )}
      </div>

      {hasRun && evalResult ? (
        <>
          <p className="text-xs text-vault-text-muted">
            Last run <span className="font-mono text-vault-text-strong">{evalResult.last_run_at}</span>{" "}
            &middot; compared with the stored baseline and floors.
          </p>
          {/* T13.J.1: row text stays plain (mockup colours only the State pill, never the whole
              row) -- the per-row red tint this used to carry is gone; StateBadge alone is the
              colour signal, icon-free but label-full ("Above floor"/"Below baseline"). */}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] border-collapse text-left text-sm">
              <thead>
                <tr>
                  <Th>Metric</Th>
                  <Th right>Value</Th>
                  <Th right>Floor</Th>
                  <Th right>Baseline</Th>
                  <Th>State</Th>
                </tr>
              </thead>
              <tbody>
                {evalResult.metrics.map((row) => {
                  const below = row.below_floor || row.regressed;
                  return (
                    <tr key={row.key} data-testid={`quality-metric-${row.key}`}>
                      <td className="border-b border-vault-row-divider px-1.5 py-2 font-mono text-vault-text-strong">
                        {row.key}
                      </td>
                      <td className="border-b border-vault-row-divider px-1.5 py-2 text-right font-mono text-vault-text-strong">
                        {row.value ?? "n/a"}
                      </td>
                      <td className="border-b border-vault-row-divider px-1.5 py-2 text-right font-mono text-vault-text-muted">
                        {row.floor ?? "n/a"}
                      </td>
                      <td className="border-b border-vault-row-divider px-1.5 py-2 text-right font-mono text-vault-text-muted">
                        {row.baseline ?? "n/a"}
                      </td>
                      <td className="border-b border-vault-row-divider px-1.5 py-2">
                        {row.value !== null && <StateBadge ok={!below} />}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        // T13.12.2: the mockup's own dashed empty-state box (Main.dc.html line 248-254) --
        // its own "Empty state (replaces ...)" line is a design-process annotation for this
        // mockup only, not real UI copy, so it is not reproduced here; the real empty-state
        // text (below) is unchanged from the pre-13-J3 behaviour.
        <div className="flex flex-col gap-1 rounded-admin-card-inner border border-dashed border-vault-steel-dark p-3.5">
          <p
            data-testid="quality-empty-state"
            className="flex items-center gap-2 text-xs text-vault-steel-light"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="#9aa3ae"
              strokeWidth={2.2}
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="8.5" />
              <path d="M8 12h8" />
            </svg>
            No evaluation recorded yet -- run the gate&apos;s eval step to get a verdict.
          </p>
        </div>
      )}

      {misspelling && (
        <p data-testid="quality-misspelling-probe" className="text-xs text-vault-text-muted">
          Misspelling tolerance probe: <span className="text-vault-amber-bright">{misspelling.status}</span>{" "}
          -- {misspelling.probe_detail ?? "no detail"}
        </p>
      )}
    </section>
  );
}
