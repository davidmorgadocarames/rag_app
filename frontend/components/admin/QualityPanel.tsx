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

import { TONE_CLASS } from "@/lib/adminColours";
import type { AdminEvalResponse, FeatureHealthOut } from "@/lib/api";

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

export default function QualityPanel({ evalResult, features }: QualityPanelProps) {
  const misspelling = features.find((f) => f.id === "quality-misspelling-probe");
  const hasRun = evalResult !== null && evalResult.last_run_at !== null;

  return (
    <section
      data-testid="admin-quality-panel"
      className="flex flex-col gap-3 rounded-lg border border-vault-line bg-vault-surface p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-text-strong">
        ANSWER QUALITY &middot; GOLDEN SET
      </h2>

      {hasRun && evalResult ? (
        <>
          <p className="text-xs text-vault-text-muted">
            Last run: <span className="font-mono text-vault-text-strong">{evalResult.last_run_at}</span>{" "}
            --{" "}
            <span className={evalResult.passed ? "text-vault-ok" : "text-vault-warn"}>
              {evalResult.passed ? "passed" : "regressed / below floor"}
            </span>
          </p>
          {/* T13.J.1: row text stays plain (mockup colours only the State pill, never the whole
              row) -- the per-row red tint this used to carry is gone; StateBadge alone is the
              colour signal, icon-free but label-full ("Above floor"/"Below baseline"). */}
          <table className="w-full text-left text-xs text-vault-steel-light">
            <thead>
              <tr className="text-vault-text-muted">
                <th>Metric</th>
                <th>Value</th>
                <th>Floor</th>
                <th>Baseline</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {evalResult.metrics.map((row) => {
                const below = row.below_floor || row.regressed;
                return (
                  <tr key={row.key} data-testid={`quality-metric-${row.key}`}>
                    <td className="font-mono text-vault-text-strong">{row.key}</td>
                    <td className="font-mono text-vault-text-strong">{row.value ?? "n/a"}</td>
                    <td className="font-mono text-vault-text-muted">{row.floor ?? "n/a"}</td>
                    <td className="font-mono text-vault-text-muted">{row.baseline ?? "n/a"}</td>
                    <td>{row.value !== null && <StateBadge ok={!below} />}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      ) : (
        <p data-testid="quality-empty-state" className="flex items-center gap-2 text-xs text-vault-text-muted">
          No evaluation recorded yet -- run the gate&apos;s eval step to get a verdict.
        </p>
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
