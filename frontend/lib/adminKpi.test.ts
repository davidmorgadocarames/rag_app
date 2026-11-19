import assert from "node:assert/strict";
import { test } from "node:test";

import {
  alertSeverityCounts,
  chatLatencySummary,
  erasureQueueSummary,
  featuresHealthySummary,
} from "./adminKpi.ts";
import type { TriageFeature } from "./adminTriage.ts";

function feature(partial: Partial<TriageFeature> & { id: string }): TriageFeature {
  return {
    probe_status: "ok",
    probe_detail: null,
    dev_only: false,
    azure_only: false,
    expected_here: false,
    ...partial,
  };
}

test("featuresHealthySummary excludes off-by-design features from the counted total", () => {
  const summary = featuresHealthySummary([
    feature({ id: "a", probe_status: "ok" }),
    feature({ id: "b", probe_status: "down" }),
    feature({ id: "c", probe_status: "down", dev_only: true }),
  ]);
  assert.deepEqual(summary, { healthy: 1, countedTotal: 2, critical: 1, warning: 0, offByDesign: 1 });
});

test("alertSeverityCounts counts red and amber independently", () => {
  assert.deepEqual(
    alertSeverityCounts([{ severity: "red" }, { severity: "amber" }, { severity: "amber" }]),
    { red: 1, amber: 2 },
  );
});

test("alertSeverityCounts on an empty list is all zero", () => {
  assert.deepEqual(alertSeverityCounts([]), { red: 0, amber: 0 });
});

test("erasureQueueSummary counts open (non-done) requests and how many are overdue", () => {
  const summary = erasureQueueSummary([
    { status: "pending", overdue: false },
    { status: "failed", overdue: true },
    { status: "done", overdue: false },
  ]);
  assert.deepEqual(summary, { pending: 2, overdue: 1 });
});

test("chatLatencySummary reads the 'total' stage's avg and finds the slowest real stage", () => {
  const summary = chatLatencySummary([
    { stage: "total", avg_seconds: 4.62 },
    { stage: "ttft", avg_seconds: 1.1 },
    { stage: "embed", avg_seconds: 0.2 },
    { stage: "generate", avg_seconds: 2.5 },
  ]);
  assert.deepEqual(summary, { avgSeconds: 4.62, slowestStage: "generate" });
});

test("chatLatencySummary never reports 'total' or 'ttft' as the slowest real stage", () => {
  const summary = chatLatencySummary([
    { stage: "total", avg_seconds: 4.62 },
    { stage: "ttft", avg_seconds: 10 },
    { stage: "embed", avg_seconds: 0.2 },
  ]);
  assert.equal(summary.slowestStage, "embed");
});

test("chatLatencySummary on no observations yet returns nulls, never a crash", () => {
  assert.deepEqual(chatLatencySummary([]), { avgSeconds: null, slowestStage: null });
});
