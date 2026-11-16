"use client";

// T13.3.5: the defence lab's admin button -- hidden entirely when the lab is off (LAB_ENABLED
// false, or ENV=prod refuses it regardless): the backend's "admin-defence-lab" feature card's
// health probe is a real route-existence check (GET /admin/lab/runs), so "down" here honestly
// means "the lab's routes are not registered on this backend" -- never a separate flag lookup.

import { useCallback, useState } from "react";

import {
  adminLabRun,
  adminLabRuns,
  type DefenceLabRunResponse,
  type DefenceRunOut,
  type FeatureHealthOut,
} from "@/lib/api";

interface DefenceLabPanelProps {
  features: FeatureHealthOut[];
}

export default function DefenceLabPanel({ features }: DefenceLabPanelProps) {
  const lab = features.find((f) => f.id === "admin-defence-lab");
  const [runs, setRuns] = useState<DefenceRunOut[]>([]);
  const [lastRun, setLastRun] = useState<DefenceLabRunResponse | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [running, setRunning] = useState(false);

  const loadRuns = useCallback(async () => {
    const response = await adminLabRuns();
    setRuns(response.runs);
    setLoaded(true);
  }, []);

  const runNow = useCallback(async () => {
    setRunning(true);
    try {
      const result = await adminLabRun();
      setLastRun(result);
      await loadRuns();
    } finally {
      setRunning(false);
    }
  }, [loadRuns]);

  if (!lab || lab.probe_status !== "ok") {
    // T13.3.1: the lab is off (or unreachable) -- never render the tab at all.
    return null;
  }

  return (
    <section
      data-testid="admin-defence-lab-panel"
      className="flex flex-col gap-2 border border-vault-steel-dark p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">DEFENCE LAB</h2>
      <p className="text-xs text-vault-steel-dark">
        THREAT_MODEL-catalogue probes, local only -- never run on Azure.
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          data-testid="defence-lab-run-button"
          disabled={running}
          onClick={() => runNow()}
          className="cursor-pointer border border-vault-steel-dark px-3 py-1 text-xs tracking-[0.1em] text-vault-steel hover:border-vault-steel disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "Running…" : "Run defence lab"}
        </button>
        {!loaded && (
          <button
            type="button"
            data-testid="defence-lab-history-button"
            onClick={() => loadRuns()}
            className="cursor-pointer border border-vault-steel-dark px-3 py-1 text-xs tracking-[0.1em] text-vault-steel hover:border-vault-steel"
          >
            Show history
          </button>
        )}
      </div>

      {lastRun && (
        <p
          data-testid="defence-lab-last-run"
          className={lastRun.all_matched ? "text-xs text-vault-steel-light" : "text-xs text-vault-danger"}
        >
          Last run @ {lastRun.git_sha.slice(0, 12)}: {lastRun.results.length} probe(s),{" "}
          {lastRun.all_matched ? "all matched" : "mismatch -- see below"}
        </p>
      )}

      {runs.length > 0 && (
        <table className="w-full text-left text-xs text-vault-steel">
          <thead>
            <tr className="text-vault-steel-dark">
              <th>Probe</th>
              <th>Observed</th>
              <th>SHA</th>
              <th>At</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((row, index) => (
              <tr
                key={`${row.probe_id}-${row.run_at}-${index}`}
                data-testid={`defence-lab-run-${row.probe_id}`}
                className={row.passed ? "" : "text-vault-danger"}
              >
                <td>{row.probe_id}</td>
                <td>{row.observed_outcome}</td>
                <td>{row.git_sha.slice(0, 8)}</td>
                <td>{row.run_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
