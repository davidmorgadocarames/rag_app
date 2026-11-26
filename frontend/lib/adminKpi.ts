// T13.8.4: pure, DOM-free KPI tile logic (C5 pattern) -- the shell's four tiles are small
// derivations over data the dashboard already fetches (`/admin/features`, `/admin/latency`,
// `/admin/erasure`), never a new backend aggregate, kept directly unit-testable here.

import { countFeaturesByStatus, type TriageFeature } from "./adminTriage.ts";
import { currencySymbol } from "./chatCost.ts";

export interface FeaturesHealthySummary {
  healthy: number;
  // The denominator excludes "off by design" features (T13.8.4): they are neither healthy
  // nor a failure, so counting them against the total would understate health for no reason.
  countedTotal: number;
  critical: number;
  warning: number;
  offByDesign: number;
}

export function featuresHealthySummary(features: TriageFeature[]): FeaturesHealthySummary {
  const counts = countFeaturesByStatus(features);
  return {
    healthy: counts.ok,
    countedTotal: features.length - counts.off,
    critical: counts.crit,
    warning: counts.warn,
    offByDesign: counts.off,
  };
}

export interface AlertSeverityCounts {
  red: number;
  amber: number;
}

export function alertSeverityCounts(alerts: { severity: string }[]): AlertSeverityCounts {
  let red = 0;
  let amber = 0;
  for (const alert of alerts) {
    if (alert.severity === "red") red += 1;
    else if (alert.severity === "amber") amber += 1;
  }
  return { red, amber };
}

// T13.J.1/ErasurePanel.tsx: an open (non-overdue) request counts as "near deadline" once less
// than 4h of its 24h SLA remain (age > 20h) -- the SAME threshold ErasurePanel's own
// `countdownClass` already used (re-exported from here, 13-J3, so the two never drift apart).
export const AMBER_THRESHOLD_SECONDS = 4 * 3600;

export interface ErasureQueueSummary {
  pending: number;
  overdue: number;
  // T13.12.2 (13-J3): the mockup's own KPI sub-line always shows BOTH clauses ("0 overdue ·
  // 1 near deadline", Main.dc.html kpis[3]) -- the pre-13-J3 code only ever showed "N
  // overdue", dropping the near-deadline count (and so never coloured the sub-line amber for
  // a queue with nothing overdue yet but something about to be).
  nearDeadline: number;
}

export function erasureQueueSummary(
  requests: { status: string; overdue: boolean; seconds_left: number }[],
): ErasureQueueSummary {
  const open = requests.filter((r) => r.status !== "done");
  const overdue = open.filter((r) => r.overdue);
  const nearDeadline = open.filter(
    (r) => !r.overdue && r.seconds_left < AMBER_THRESHOLD_SECONDS,
  );
  return { pending: open.length, overdue: overdue.length, nearDeadline: nearDeadline.length };
}

export interface ChatLatencySummary {
  avgSeconds: number | null;
  slowestStage: string | null;
}

// These two stage names are bookkeeping, not real pipeline work (T11.3.1's `recorder.emit`) --
// never eligible to be reported as "the slowest step".
const NON_WORK_STAGES = new Set(["total", "ttft"]);

export function chatLatencySummary(
  stages: { stage: string; avg_seconds: number | null }[],
): ChatLatencySummary {
  const total = stages.find((s) => s.stage === "total");
  let slowestStage: string | null = null;
  let slowestSeconds = -Infinity;
  for (const s of stages) {
    if (NON_WORK_STAGES.has(s.stage) || s.avg_seconds === null) continue;
    if (s.avg_seconds > slowestSeconds) {
      slowestSeconds = s.avg_seconds;
      slowestStage = s.stage;
    }
  }
  return { avgSeconds: total?.avg_seconds ?? null, slowestStage };
}

// DEBT-065: the Quality nav tag must reflect whether an eval has actually been RECORDED on
// this replica (`last_run_at`), never `evalResult.passed` alone -- the backend's own
// `admin_eval()` defaults `passed=True` when nothing has been observed at all (every metric
// value is `null`, so nothing trips below_floor/regressed), so a tag computed from `passed`
// alone said "passed" on a replica with NO evaluation recorded, directly contradicting the
// SAME response's own `QualityPanel` body ("No evaluation recorded yet"). Pulled out as its
// own pure function (same C5 pattern as the rest of this file) so the nav tag and the panel
// body can never drift apart again -- both now gate on the identical `last_run_at` check.
export function qualityNavTag(
  evalResult: { last_run_at: string | null; passed: boolean } | null,
): string {
  if (evalResult === null || evalResult.last_run_at === null) return "no data";
  return evalResult.passed ? "passed" : "regressed";
}

// T14.5.4: the Pipeline panel's aggregate cost line -- "aggregate, no per-user data, no
// message content" (PHASE_PLANNING 14.5). A local Ollama replica always shows 0 (see
// rag_app.cost.estimate_cost); the SAME currencySymbol lookup the chat footer uses.
export function formatAggregateCost(total: number, currency: string): string {
  const symbol = currencySymbol(currency);
  return total > 0 ? `${symbol}${total.toFixed(4)}` : `${symbol}0`;
}
