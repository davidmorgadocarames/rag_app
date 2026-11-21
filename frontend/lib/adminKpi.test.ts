import assert from "node:assert/strict";
import { test } from "node:test";

import {
  alertSeverityCounts,
  AMBER_THRESHOLD_SECONDS,
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
    { status: "pending", overdue: false, seconds_left: 18 * 3600 },
    { status: "failed", overdue: true, seconds_left: -1800 },
    { status: "done", overdue: false, seconds_left: 0 },
  ]);
  assert.deepEqual(summary, { pending: 2, overdue: 1, nearDeadline: 0 });
});

// T13.12.2: the mockup's KPI sub-line always shows both "overdue" and "near deadline" (a
// non-overdue open request with less than AMBER_THRESHOLD_SECONDS left) -- fails on the
// pre-13-J3 code, which never computed this at all.
test("erasureQueueSummary counts a non-overdue request under the amber threshold as near deadline", () => {
  const summary = erasureQueueSummary([
    { status: "pending", overdue: false, seconds_left: AMBER_THRESHOLD_SECONDS - 1 },
    { status: "pending", overdue: false, seconds_left: AMBER_THRESHOLD_SECONDS + 1 },
    { status: "failed", overdue: true, seconds_left: -1 },
  ]);
  assert.deepEqual(summary, { pending: 3, overdue: 1, nearDeadline: 1 });
});

test("erasureQueueSummary never counts a done request as near deadline even with little time left", () => {
  const summary = erasureQueueSummary([
    { status: "done", overdue: false, seconds_left: 1 },
  ]);
  assert.deepEqual(summary, { pending: 0, overdue: 0, nearDeadline: 0 });
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
