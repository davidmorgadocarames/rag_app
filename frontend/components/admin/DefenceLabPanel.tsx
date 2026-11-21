"use client";

// T13.3.5/T13.8.8 (13-G3): the defence lab's admin button -- hidden entirely when the lab is
// off (LAB_ENABLED false, or ENV=prod refuses it regardless): the backend's "admin-defence-lab"
// feature card's health probe is a real route-existence check (GET /admin/lab/runs), so "down"
// here honestly means "the lab's routes are not registered on this backend" -- never a
// separate flag lookup.
//
// T13.8.8: restyled as a card grid using the SAME status badges the rest of the dashboard
// uses (StatusBadge, "ok"/"crit" tones) -- "OK = the defence held" is the same vocabulary as
// the Triage section, not a separate one invented just for this panel.

import { useCallback, useState } from "react";

import {
  adminLabRun,
  adminLabRuns,
  type DefenceLabRunResponse,
  type DefenceProbeResultOut,
  type DefenceRunOut,
  type FeatureHealthOut,
} from "@/lib/api";

import StatusBadge from "./StatusBadge";

interface DefenceLabPanelProps {
  features: FeatureHealthOut[];
}

interface ProbeCard {
  probeId: string;
  passed: boolean;
  observedOutcome: string;
  lastAt: string | null;
}

/** Builds one card per probe from whichever data is freshest: the just-finished run's own
 * results (`lastRun`) take priority over the history list, and within the history list only
 * the MOST RECENT run per probe is kept (the list may contain many past runs per probe). */
function buildCards(lastRun: DefenceLabRunResponse | null, history: DefenceRunOut[]): ProbeCard[] {
  if (lastRun) {
    return lastRun.results.map((r: DefenceProbeResultOut) => ({
      probeId: r.probe_id,
      passed: r.passed,
      observedOutcome: r.observed_outcome,
      lastAt: null,
    }));
  }
  const latestByProbe = new Map<string, DefenceRunOut>();
  for (const run of history) {
    const prev = latestByProbe.get(run.probe_id);
    if (!prev || run.run_at > prev.run_at) latestByProbe.set(run.probe_id, run);
  }
  return [...latestByProbe.values()]
    .sort((a, b) => a.probe_id.localeCompare(b.probe_id))
    .map((r) => ({
      probeId: r.probe_id,
      passed: r.passed,
      observedOutcome: r.observed_outcome,
      lastAt: r.run_at,
    }));
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

  const cards = buildCards(lastRun, runs);

  return (
    <section
      data-testid="admin-defence-lab-panel"
      className="flex flex-col gap-3.5 rounded-admin-card border border-vault-line bg-vault-surface p-[18px]"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm tracking-[0.18em] text-vault-text-strong">DEFENCE LAB</h2>
        <span className="rounded-full border border-vault-warn-border px-2.5 py-0.5 text-xs text-vault-warn">
          Local stack only &middot; dev routes
        </span>
      </div>
      <p className="text-xs text-vault-text-muted">
        Each probe attacks this local stack on purpose and expects the defence to hold. Same
        status language as the Triage section: OK = the defence held.
      </p>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          data-testid="defence-lab-run-button"
          disabled={running}
          onClick={() => runNow()}
          className="min-h-9 cursor-pointer rounded-admin-btn border border-vault-steel-dark px-3 py-1.5 text-xs tracking-[0.1em] text-vault-amber-bright hover:border-vault-steel disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "Running…" : "Run defence lab"}
        </button>
        {!loaded && (
          <button
            type="button"
            data-testid="defence-lab-history-button"
            onClick={() => loadRuns()}
            className="min-h-9 cursor-pointer rounded-admin-btn border border-vault-steel-dark px-3 py-1.5 text-xs tracking-[0.1em] text-vault-steel-light hover:border-vault-steel"
          >
            Show history
          </button>
        )}
      </div>

      {lastRun && (
        <p
          data-testid="defence-lab-last-run"
          className={lastRun.all_matched ? "text-xs text-vault-ok" : "text-xs text-vault-crit"}
        >
          Last run @ {lastRun.git_sha.slice(0, 12)}: {lastRun.results.length} probe(s),{" "}
          {lastRun.all_matched ? "all matched" : "mismatch -- see below"}
        </p>
      )}

      {cards.length === 0 ? (
        <p data-testid="defence-lab-empty-state" className="text-xs text-vault-text-muted">
          No probes run yet on this replica -- press &quot;Run defence lab&quot; or &quot;Show
          history&quot;.
        </p>
      ) : (
        <div
          data-testid="defence-lab-cards"
          // T13.12.2 (13-J3): the mockup's own `grid-template-columns: repeat(auto-fit,
          // minmax(260px, 1fr))` (Main.dc.html line 311) -- fluid column count by available
          // width, not fixed Tailwind breakpoints; `gap-2.5` (10px) matches the mockup's gap.
          className="grid grid-cols-[repeat(auto-fit,minmax(260px,1fr))] gap-2.5"
        >
          {cards.map((card) => (
            <div
              key={card.probeId}
              data-testid={`defence-lab-card-${card.probeId}`}
              // T13.12.2: `p-3.5` (14px) matches the mockup's own probe card padding exactly
              // (Main.dc.html lab card: `padding: 14px`).
              className="flex flex-col gap-2 rounded-admin-card-inner border border-vault-line bg-vault-surface-raised p-3.5"
            >
              <div className="flex items-center justify-between gap-2">
                {/* T13.12.2: `font-semibold` (600) matches the mockup's own `p.name` weight --
                    stays `font-mono` regardless (the app's monospace-everywhere exception; no
                    separate human-readable probe name exists on the wire, `probe_id` is it). */}
                <span className="font-mono text-xs font-semibold text-vault-text-strong">
                  {card.probeId}
                </span>
                <StatusBadge tone={card.passed ? "ok" : "crit"} label={card.passed ? "Held" : "Mismatch"} />
              </div>
              <span className="text-xs text-vault-text-muted">{card.observedOutcome}</span>
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs text-vault-text-muted">{card.lastAt ?? ""}</span>
                {/* T13.12.2: the mockup's own per-card "Run probe" button -- the real backend
                    has no per-probe run route (adminLabRun() always runs the whole lab), so
                    this wires to the SAME existing global action as the button above, just
                    exposed per-card to match the mockup's layout -- no new behaviour. */}
                <button
                  type="button"
                  data-testid={`defence-lab-card-run-${card.probeId}`}
                  disabled={running}
                  onClick={() => runNow()}
                  className="min-h-9 cursor-pointer rounded-admin-btn border border-vault-steel-dark px-3 py-1.5 text-xs text-vault-amber-bright hover:border-vault-steel disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Run probe
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
