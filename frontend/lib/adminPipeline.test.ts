import assert from "node:assert/strict";
import { test } from "node:test";

import { buildPipelineWaterfall, waterfallBarPercent } from "./adminPipeline.ts";
import type { PipelineStageLatencyOut, StepLatencyOut } from "./api.ts";

function step(
  stage: string,
  stepName: string,
  avg: number | null,
  count = avg === null ? 0 : 1,
  p95: number | null = avg,
): StepLatencyOut {
  return { stage, step: stepName, count, avg_seconds: avg, p50_seconds: avg, p95_seconds: p95 };
}

test("buildPipelineWaterfall: a stage's total is the SUM of its own steps, never a separate field (DA-13-G1-1)", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    {
      stage: "retrieving",
      steps: [
        step("retrieving", "embed_query", 0.1),
        step("retrieving", "vector_search", 0.2),
        step("retrieving", "load_chunks", 0.05),
      ],
    },
  ];
  const result = buildPipelineWaterfall(pipeline);
  assert.equal(result.stages[0].totalAvgSeconds, 0.1 + 0.2 + 0.05);
});

test("buildPipelineWaterfall: consecutive RAG-path stages start where the previous one ends", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    { stage: "classifying", steps: [step("classifying", "classify_intent", 0.04)] },
    { stage: "retrieving", steps: [step("retrieving", "embed_query", 0.1)] },
  ];
  const result = buildPipelineWaterfall(pipeline);
  assert.equal(result.stages[0].startSeconds, 0);
  assert.equal(result.stages[1].startSeconds, 0.04);
  assert.equal(result.stages[1].steps[0].startSeconds, 0.04);
});

test("buildPipelineWaterfall: the chit-chat 'responding' stage is drawn from 0 and excluded from the RAG total", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    { stage: "classifying", steps: [step("classifying", "classify_intent", 0.04)] },
    { stage: "retrieving", steps: [step("retrieving", "embed_query", 0.1)] },
    { stage: "responding", steps: [step("responding", "stream_canned_reply", 0.02)] },
  ];
  const result = buildPipelineWaterfall(pipeline);
  const responding = result.stages.find((s) => s.stage === "responding");
  assert.equal(responding?.startSeconds, 0);
  assert.equal(responding?.drawnFromZero, true);
  // Total only covers classifying + retrieving, never + responding's 0.02.
  assert.equal(result.totalAvgSeconds, 0.04 + 0.1);
});

test("buildPipelineWaterfall: exactly one globally slowest OBSERVED step is flagged, across every stage", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    {
      stage: "retrieving",
      steps: [step("retrieving", "embed_query", 0.1), step("retrieving", "vector_search", 0.2)],
    },
    {
      stage: "generating",
      steps: [step("generating", "token_stream", 1.5), step("generating", "build_prompt", 0.01)],
    },
  ];
  const result = buildPipelineWaterfall(pipeline);
  const slowest = result.stages.flatMap((s) => s.steps).filter((s) => s.isSlowest);
  assert.equal(slowest.length, 1);
  assert.equal(slowest[0].step, "token_stream");
});

test("buildPipelineWaterfall: an unobserved step (count 0) is never the slowest, even if it carries a stale avg", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    {
      stage: "generating",
      // count 0 but a non-null avg_seconds would still type-check against the schema
      // (null averages are the common case, but nothing stops a 0-count/0-avg default).
      steps: [step("generating", "token_stream", 5, 0), step("generating", "build_prompt", 0.01, 1)],
    },
  ];
  const result = buildPipelineWaterfall(pipeline);
  const slowest = result.stages[0].steps.find((s) => s.isSlowest);
  assert.equal(slowest?.step, "build_prompt");
});

test("buildPipelineWaterfall: hasAnyData is false on a freshly started replica (every step count 0) -- the honest empty state", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    { stage: "classifying", steps: [step("classifying", "classify_intent", null, 0)] },
    { stage: "responding", steps: [step("responding", "stream_canned_reply", null, 0)] },
  ];
  const result = buildPipelineWaterfall(pipeline);
  assert.equal(result.hasAnyData, false);
  assert.equal(result.totalAvgSeconds, 0);
});

test("buildPipelineWaterfall: hasAnyData is true once at least one step has been observed", () => {
  const pipeline: PipelineStageLatencyOut[] = [
    { stage: "classifying", steps: [step("classifying", "classify_intent", 0.03, 1)] },
  ];
  assert.equal(buildPipelineWaterfall(pipeline).hasAnyData, true);
});

test("waterfallBarPercent: a normal bar's left/width are plain percentages of the total", () => {
  const { leftPercent, widthPercent } = waterfallBarPercent(1, 2, 10);
  assert.equal(leftPercent, 10);
  assert.equal(widthPercent, 20);
});

test("waterfallBarPercent: a very short duration is clamped to a visible minimum width, never literally 0", () => {
  const { widthPercent } = waterfallBarPercent(0, 0.0001, 100);
  assert.ok(widthPercent >= 0.6);
});

test("waterfallBarPercent: zero total (no data yet) never divides by zero", () => {
  const result = waterfallBarPercent(0, 0, 0);
  assert.deepEqual(result, { leftPercent: 0, widthPercent: 0 });
});

test("waterfallBarPercent: zero-duration step at a real offset keeps its left position with zero width", () => {
  const result = waterfallBarPercent(5, 0, 10);
  assert.deepEqual(result, { leftPercent: 50, widthPercent: 0 });
});
