// T13.8.7 (13-G3): pure, DOM-free helpers for the Pipeline section's average waterfall -- same
// C5 pattern as adminTriage.ts/adminKpi.ts, kept directly unit-testable without React/Recharts.
//
// DA-13-G1-1's own note (carried into this block's brief): a stage's waterfall total is
// ALWAYS the SUM of its own atomic steps read from the `pipeline` field
// (`sum(step.avg_seconds for step in stage.steps)`) -- NEVER reconciled against the separate,
// higher-level `AdminLatencyResponse.stages[].avg_seconds` field for the "same" stage name. The
// two are allowed to disagree by a small amount (untracked/duplicated-cost gaps the 13-G1-fix
// narrowed but did not fully close -- see the ADR "Pipeline waterfall semantics" section) --
// reconciling them here would make the parent bar and its own children visibly disagree.

import type { PipelineStageLatencyOut, StepLatencyOut } from "@/lib/api";

// The chit-chat fast path's own UI stage: a SEPARATE path from the RAG pipeline
// (classifying -> retrieving -> reranking -> generating -> checking), not a continuation of
// it, so its bar is drawn starting at 0 and it is excluded from the RAG path's own total --
// summing the two paths together would produce a number that means nothing.
export const DRAWN_FROM_ZERO_STAGES: ReadonlySet<string> = new Set(["responding"]);

export interface WaterfallStep {
  step: string;
  avgSeconds: number;
  p95Seconds: number | null;
  count: number;
  observed: boolean;
  startSeconds: number;
  isSlowest: boolean;
}

export interface WaterfallStage {
  stage: string;
  steps: WaterfallStep[];
  totalAvgSeconds: number;
  totalP95Seconds: number;
  startSeconds: number;
  drawnFromZero: boolean;
  observedCount: number;
}

export interface PipelineWaterfall {
  stages: WaterfallStage[];
  totalAvgSeconds: number;
  hasAnyData: boolean;
}

function stepAvg(step: StepLatencyOut): number {
  return step.avg_seconds ?? 0;
}

/** Builds the average waterfall straight from `AdminLatencyResponse.pipeline`, in the order
 * the backend already returns it (`metrics.STEP_ALLOWLIST`'s own pipeline order). The single
 * globally slowest OBSERVED step (across every stage, not per-stage) is flagged
 * `isSlowest` -- an unobserved (count 0) step is never considered, however its `avg_seconds`
 * value happens to default. */
export function buildPipelineWaterfall(pipeline: PipelineStageLatencyOut[]): PipelineWaterfall {
  const observedAvgs = pipeline
    .flatMap((s) => s.steps)
    .filter((s) => s.count > 0)
    .map(stepAvg);
  const maxAvg = observedAvgs.length > 0 ? Math.max(...observedAvgs) : null;
  let slowestAssigned = false;

  let cursor = 0;
  const stages: WaterfallStage[] = pipeline.map((stageEntry) => {
    const drawnFromZero = DRAWN_FROM_ZERO_STAGES.has(stageEntry.stage);
    const start = drawnFromZero ? 0 : cursor;
    let within = 0;
    const steps: WaterfallStep[] = stageEntry.steps.map((step) => {
      const avg = stepAvg(step);
      // `=== maxAvg` would highlight every tied step; only the FIRST one in pipeline order
      // is flagged, so "slowest step" always names exactly one step.
      const isSlowest =
        !slowestAssigned && step.count > 0 && maxAvg !== null && avg === maxAvg;
      if (isSlowest) slowestAssigned = true;
      const built: WaterfallStep = {
        step: step.step,
        avgSeconds: avg,
        p95Seconds: step.p95_seconds,
        count: step.count,
        observed: step.count > 0,
        startSeconds: start + within,
        isSlowest,
      };
      within += avg;
      return built;
    });
    const totalP95Seconds = stageEntry.steps.reduce((t, s) => t + (s.p95_seconds ?? 0), 0);
    const observedCount = stageEntry.steps.reduce((m, s) => Math.max(m, s.count), 0);
    if (!drawnFromZero) cursor += within;
    return {
      stage: stageEntry.stage,
      steps,
      totalAvgSeconds: within,
      totalP95Seconds,
      startSeconds: start,
      drawnFromZero,
      observedCount,
    };
  });

  const totalAvgSeconds = stages
    .filter((s) => !s.drawnFromZero)
    .reduce((t, s) => t + s.totalAvgSeconds, 0);
  const hasAnyData = stages.some((s) => s.observedCount > 0);

  return { stages, totalAvgSeconds, hasAnyData };
}

/** A bar's left offset + width as percentages of `totalSeconds`, clamped to a visible minimum
 * (0.6%, same convention as the approved mockup) so a genuinely fast step is never literally
 * invisible. `totalSeconds <= 0` (no data at all) returns a zero-width bar instead of
 * dividing by zero. */
export function waterfallBarPercent(
  startSeconds: number,
  durationSeconds: number,
  totalSeconds: number,
): { leftPercent: number; widthPercent: number } {
  if (totalSeconds <= 0 || durationSeconds <= 0) {
    return { leftPercent: totalSeconds > 0 ? (startSeconds / totalSeconds) * 100 : 0, widthPercent: 0 };
  }
  return {
    leftPercent: (startSeconds / totalSeconds) * 100,
    widthPercent: Math.max((durationSeconds / totalSeconds) * 100, 0.6),
  };
}
