// Unit tests for the wake-pipeline step reducer (T13.12.6/T13.12.7, DEBT-062): pure
// `events -> step states`, `node --test` (no DOM, no timers -- `now` is injected).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  WAKE_STEP_ORDER,
  type WakeStepEvent,
  type WakeStepId,
  type WakeStepsState,
  initialWakeStepsState,
  reduceWakeStep,
} from "./wakeSteps.ts";

function statusOf(state: WakeStepsState, id: WakeStepId): string {
  return state.statuses[id];
}

function run(events: WakeStepEvent[], startAt = 0): WakeStepsState {
  let state = initialWakeStepsState(startAt);
  let t = startAt;
  for (const e of events) {
    t += 1;
    state = reduceWakeStep(state, e, t);
  }
  return state;
}

test("initial state: backend is current, everything else pending", () => {
  const state = initialWakeStepsState(0);
  assert.equal(state.current, "backend");
  assert.equal(statusOf(state, "backend"), "current");
  for (const id of WAKE_STEP_ORDER.slice(1)) assert.equal(statusOf(state, id), "pending");
});

test("the very first event (any kind) retires backend; nothing is current yet", () => {
  const state = run([{ kind: "other" }]);
  assert.equal(statusOf(state, "backend"), "done");
  assert.equal(state.current, null);
});

test("a 'classifying'-shaped stage event also just retires backend (no box row of its own)", () => {
  const state = run([{ kind: "stage", stage: "classifying" }]);
  assert.equal(statusOf(state, "backend"), "done");
  assert.equal(state.current, null);
});

test("embed_query start makes 'embed' current and backend done", () => {
  const state = run([
    { kind: "stage", stage: "classifying" },
    { kind: "stage", stage: "retrieving" },
    { kind: "step", step: "embed_query", phase: "start" },
  ]);
  assert.equal(statusOf(state, "backend"), "done");
  assert.equal(statusOf(state, "embed"), "current");
  assert.equal(state.current, "embed");
  assert.equal(statusOf(state, "retrieving"), "pending");
});

test("embed_query end moves to 'retrieving': embed done, retrieving current", () => {
  const state = run([
    { kind: "stage", stage: "retrieving" },
    { kind: "step", step: "embed_query", phase: "start" },
    { kind: "step", step: "embed_query", phase: "end" },
  ]);
  assert.equal(statusOf(state, "embed"), "done");
  assert.equal(statusOf(state, "retrieving"), "current");
  assert.equal(state.current, "retrieving");
});

test("the full security path (with reranking) walks every step in order, ending at checking", () => {
  const state = run([
    { kind: "stage", stage: "classifying" },
    { kind: "stage", stage: "retrieving" },
    { kind: "step", step: "embed_query", phase: "start" },
    { kind: "step", step: "embed_query", phase: "end" },
    { kind: "stage", stage: "reranking" },
    { kind: "stage", stage: "generating" },
    { kind: "stage", stage: "checking" },
  ]);
  for (const id of ["backend", "embed", "retrieving", "reranking", "generating"] as WakeStepId[]) {
    assert.equal(statusOf(state, id), "done", id);
  }
  assert.equal(statusOf(state, "checking"), "current");
  assert.equal(state.current, "checking");
});

test("no rerank (skipped stage): generating marks reranking done too, never stuck pending", () => {
  const state = run([
    { kind: "stage", stage: "retrieving" },
    { kind: "step", step: "embed_query", phase: "start" },
    { kind: "step", step: "embed_query", phase: "end" },
    { kind: "stage", stage: "generating" },
  ]);
  assert.equal(statusOf(state, "reranking"), "done");
  assert.equal(statusOf(state, "generating"), "current");
});

test("chit-chat path ('responding') is treated as generating -- no embed/retrieving ever touched", () => {
  const state = run([
    { kind: "stage", stage: "classifying" },
    { kind: "stage", stage: "responding" },
  ]);
  assert.equal(statusOf(state, "embed"), "done");
  assert.equal(statusOf(state, "retrieving"), "done");
  assert.equal(statusOf(state, "reranking"), "done");
  assert.equal(statusOf(state, "generating"), "current");
});

test("a stale/out-of-order event (behind the furthest step already reached) is a no-op", () => {
  const afterReranking = run([
    { kind: "stage", stage: "retrieving" },
    { kind: "step", step: "embed_query", phase: "start" },
    { kind: "step", step: "embed_query", phase: "end" },
    { kind: "stage", stage: "reranking" },
  ]);
  const replayed = reduceWakeStep(afterReranking, { kind: "step", step: "embed_query", phase: "start" }, 999);
  assert.deepEqual(replayed, afterReranking);
});

test("an unknown step name is a forward-compat no-op (besides retiring backend)", () => {
  const state = run([{ kind: "step", step: "some_future_step", phase: "start" }]);
  assert.equal(statusOf(state, "backend"), "done");
  assert.equal(state.current, null);
  for (const id of WAKE_STEP_ORDER.slice(1)) assert.equal(statusOf(state, id), "pending");
});

test("currentSince tracks the 'now' passed in when a step becomes current", () => {
  let state = initialWakeStepsState(100);
  state = reduceWakeStep(state, { kind: "stage", stage: "retrieving" }, 150);
  assert.equal(state.current, null); // "retrieving" the stage alone has no box row
  state = reduceWakeStep(state, { kind: "step", step: "embed_query", phase: "start" }, 200);
  assert.equal(state.current, "embed");
  assert.equal(state.currentSince, 200);
});
