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
//
// T13.J4-fix (DA-13-J3J4-1/-2): each card now has a REAL per-probe run route
// (`POST /admin/lab/run/{probe_id}`) -- pressing one card's button runs ONLY that probe,
// never the whole lab (the 13-J3 panel wired every card to the same whole-lab action, which
// silently re-ran and refreshed every OTHER card too). Cards also show the catalogue's own
// human name + a static one-line "what this attacks" description (the mockup's `p.name`/
// `p.what`), falling back to the raw id when either is missing (an older backend, or a
// seeded e2e fixture).

import { useCallback, useState } from "react";

import {
  adminLabRun,
  adminLabRunProbe,
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
  name: string;
  description: string;
  passed: boolean;
  observedOutcome: string;
  lastAt: string | null;
}

function cardFromResult(r: DefenceProbeResultOut, lastAt: string | null): ProbeCard {
  return {
    probeId: r.probe_id,
    name: r.name && r.name.length > 0 ? r.name : r.probe_id,
    description: r.description ?? "",
    passed: r.passed,
    observedOutcome: r.observed_outcome,
    lastAt,
  };
}

function cardFromHistoryRow(row: DefenceRunOut): ProbeCard {
  return {
    probeId: row.probe_id,
    name: row.name && row.name.length > 0 ? row.name : row.probe_id,
    description: row.description ?? "",
    passed: row.passed,
    observedOutcome: row.observed_outcome,
    lastAt: row.run_at,
  };
}

/** The most recent history row per probe id (the list may contain many past runs per
 * probe) -- same dedup rule as before T13.J4-fix, just factored out so `loadRuns` can merge
 * it into the persistent per-probe card map below instead of replacing it wholesale. */
function latestHistoryByProbe(history: DefenceRunOut[]): Map<string, DefenceRunOut> {
  const latest = new Map<string, DefenceRunOut>();
  for (const run of history) {
    const prev = latest.get(run.probe_id);
    if (!prev || run.run_at > prev.run_at) latest.set(run.probe_id, run);
  }
  return latest;
}

export default function DefenceLabPanel({ features }: DefenceLabPanelProps) {
  const lab = features.find((f) => f.id === "admin-defence-lab");
  // T13.J4-fix: a PERSISTENT per-probe map, keyed by probe id -- a whole-lab run overwrites
  // every entry it returns; a single-probe run overwrites ONLY that one entry; `loadRuns`
  // only fills in ids this session has no fresher entry for yet. This is what actually
  // guarantees "running one probe never touches any other card" -- the old code recomputed
  // every card from `lastRun` alone, which only ever held a WHOLE run's results.
  const [cards, setCards] = useState<Map<string, ProbeCard>>(new Map());
  const [loaded, setLoaded] = useState(false);
  // Set ONLY by the whole-lab run (never the per-probe one, DA-13-J3J4-1): a single-probe
  // run must never render the "Last run @ sha: N probe(s), all matched" summary line, which
  // would misleadingly imply the whole lab just ran.
  const [lastRun, setLastRun] = useState<DefenceLabRunResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [runningProbeId, setRunningProbeId] = useState<string | null>(null);
  const anyRunInProgress = running || runningProbeId !== null;

  const mergeResults = useCallback((results: DefenceProbeResultOut[], lastAt: string | null) => {
    setCards((prev) => {
      const next = new Map(prev);
      for (const r of results) next.set(r.probe_id, cardFromResult(r, lastAt));
      return next;
    });
  }, []);

  const loadRuns = useCallback(async () => {
    const response = await adminLabRuns();
    const latest = latestHistoryByProbe(response.runs);
    setCards((prev) => {
      const next = new Map(prev);
      for (const [probeId, row] of latest) {
        if (!next.has(probeId)) next.set(probeId, cardFromHistoryRow(row));
      }
      return next;
    });
    setLoaded(true);
  }, []);

  const runNow = useCallback(async () => {
    setRunning(true);
    try {
      const result = await adminLabRun();
      setLastRun(result);
      mergeResults(result.results, null);
    } finally {
      setRunning(false);
    }
  }, [mergeResults]);

  const runProbe = useCallback(
    async (probeId: string) => {
      setRunningProbeId(probeId);
      try {
        const result = await adminLabRunProbe(probeId);
        mergeResults(result.results, null);
      } finally {
        setRunningProbeId(null);
      }
    },
    [mergeResults],
  );

  if (!lab || lab.probe_status !== "ok") {
    // T13.3.1: the lab is off (or unreachable) -- never render the tab at all.
    return null;
  }

  const sortedCards = [...cards.values()].sort((a, b) => a.probeId.localeCompare(b.probeId));

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
          disabled={anyRunInProgress}
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

      {sortedCards.length === 0 ? (
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
          {sortedCards.map((card) => (
            <div
              key={card.probeId}
              data-testid={`defence-lab-card-${card.probeId}`}
              // T13.12.2: `p-3.5` (14px) matches the mockup's own probe card padding exactly
              // (Main.dc.html lab card: `padding: 14px`).
              className="flex flex-col gap-2 rounded-admin-card-inner border border-vault-line bg-vault-surface-raised p-3.5"
            >
              <div className="flex items-start justify-between gap-2">
                {/* T13.12.2/DA-13-J3J4-2: `p.name` is the mockup's primary heading
                    (`font-semibold`, the mockup's own weight 600) -- stays `font-mono` (the
                    app's monospace-everywhere exception). The raw catalogue id is kept as a
                    smaller secondary mono line underneath, never dropped entirely. */}
                <div className="flex flex-col">
                  <span className="font-mono text-xs font-semibold text-vault-text-strong">
                    {card.name}
                  </span>
                  <span className="font-mono text-[10px] text-vault-text-muted">
                    {card.probeId}
                  </span>
                </div>
                <StatusBadge tone={card.passed ? "ok" : "crit"} label={card.passed ? "Held" : "Mismatch"} />
              </div>
              {card.description && (
                // DA-13-J3J4-2: `p.what` -- a static one-line "what this attacks" summary,
                // a different fact from `observedOutcome` below (the latest RUN's outcome).
                <span className="text-xs text-vault-text-muted">{card.description}</span>
              )}
              <span className="text-xs text-vault-text-muted">{card.observedOutcome}</span>
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs text-vault-text-muted">{card.lastAt ?? ""}</span>
                {/* T13.J4-fix (DA-13-J3J4-1): the mockup's own per-card "Run probe" button --
                    now a REAL per-probe route (POST /admin/lab/run/{probe_id}); pressing it
                    never touches any other card. */}
                <button
                  type="button"
                  data-testid={`defence-lab-card-run-${card.probeId}`}
                  disabled={anyRunInProgress}
                  onClick={() => runProbe(card.probeId)}
                  className="min-h-9 cursor-pointer rounded-admin-btn border border-vault-steel-dark px-3 py-1.5 text-xs text-vault-amber-bright hover:border-vault-steel disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {runningProbeId === card.probeId ? "Running…" : "Run probe"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
