"use client";

// T13.2.7: quality panel -- the last eval run vs. baseline and floors (T13.1.4), plus the
// misspelling probe's own expected-fail status (Phase 14 gap, tracked honestly rather than
// hidden) read from the feature card's health probe.

import type { AdminEvalResponse, FeatureHealthOut } from "@/lib/api";

interface QualityPanelProps {
  evalResult: AdminEvalResponse | null;
  features: FeatureHealthOut[];
}

export default function QualityPanel({ evalResult, features }: QualityPanelProps) {
  const misspelling = features.find((f) => f.id === "quality-misspelling-probe");

  return (
    <section
      data-testid="admin-quality-panel"
      className="flex flex-col gap-3 border border-vault-steel-dark p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">QUALITY</h2>

      {evalResult ? (
        <>
          <p className="text-xs text-vault-steel-dark">
            Last run: {evalResult.last_run_at ?? "never"} --{" "}
            <span className={evalResult.passed ? "text-vault-steel-light" : "text-vault-danger"}>
              {evalResult.passed ? "passed" : "regressed / below floor"}
            </span>
          </p>
          <table className="w-full text-left text-xs text-vault-steel">
            <thead>
              <tr className="text-vault-steel-dark">
                <th>Metric</th>
                <th>Value</th>
                <th>Floor</th>
                <th>Baseline</th>
              </tr>
            </thead>
            <tbody>
              {evalResult.metrics.map((row) => (
                <tr
                  key={row.key}
                  data-testid={`quality-metric-${row.key}`}
                  className={row.below_floor || row.regressed ? "text-vault-danger" : ""}
                >
                  <td>{row.key}</td>
                  <td>{row.value ?? "n/a"}</td>
                  <td>{row.floor ?? "n/a"}</td>
                  <td>{row.baseline ?? "n/a"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="text-xs text-vault-steel-dark">Loading…</p>
      )}

      {misspelling && (
        <p data-testid="quality-misspelling-probe" className="text-xs text-vault-steel-dark">
          Misspelling tolerance probe: <span className="text-vault-amber">{misspelling.status}</span>{" "}
          -- {misspelling.probe_detail ?? "no detail"}
        </p>
      )}
    </section>
  );
}
