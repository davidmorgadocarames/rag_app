// Unit tests for the chat UI's visible-plan reducer (T14.4.1/T14.4.2/T14.4.3, block 14-D2):
// pure `events -> entries`, `node --test`. Every test here fails on pre-14-D2 code
// (`./planSteps.ts` does not exist yet).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  type PlanStepEvent,
  type PlanStepsState,
  initialPlanStepsState,
  reducePlanStep,
} from "./planSteps.ts";

function run(events: PlanStepEvent[]): PlanStepsState {
  let state = initialPlanStepsState();
  for (const e of events) state = reducePlanStep(state, e);
  return state;
}

test("initial state has no entries", () => {
  assert.deepEqual(initialPlanStepsState(), { entries: [] });
});

test("'understood' becomes a labelled entry with the intent/language detail", () => {
  const state = run([{ kind: "step", step: "understood", detail: "security/en" }]);
  assert.deepEqual(state.entries, [{ id: "understood", label: "Understood: security/en" }]);
});

test("the full security-path sequence appends one entry per recognised step, in order", () => {
  const state = run([
    { kind: "step", step: "understood", detail: "security/en" },
    { kind: "step", step: "searching", detail: "How do I prevent SQLi? (2021)" },
    { kind: "step", step: "found_sources", detail: "3" },
    { kind: "step", step: "writing_answer" },
  ]);
  assert.deepEqual(
    state.entries.map((e) => e.label),
    [
      "Understood: security/en",
      "Searching: How do I prevent SQLi? (2021)",
      "Found 3 sources",
      "Writing answer",
    ],
  );
});

test("found_sources with no detail (defensive) still reports a count, never blank", () => {
  const state = run([{ kind: "step", step: "found_sources" }]);
  assert.equal(state.entries[0].label, "Found 0 sources");
});

test("a chit-chat turn gets exactly the 'understood' entry, nothing else", () => {
  const state = run([{ kind: "step", step: "understood", detail: "chitchat/en" }]);
  assert.equal(state.entries.length, 1);
});

test("an unrecognised step name (e.g. the pre-existing embed_query) is a forward-compat no-op", () => {
  const state = run([
    { kind: "step", step: "understood", detail: "security/en" },
    { kind: "step", step: "embed_query", detail: undefined },
  ]);
  assert.equal(state.entries.length, 1);
});

test("'reset' clears every accumulated entry back to the initial state", () => {
  const withEntries = run([{ kind: "step", step: "understood", detail: "security/en" }]);
  const afterReset = reducePlanStep(withEntries, { kind: "reset" });
  assert.deepEqual(afterReset, initialPlanStepsState());
});

test("the server's own XSS-shaped standalone_query text is kept as a literal label string -- never parsed as HTML by this reducer (rendering safety is PlanPanel.tsx's job)", () => {
  const malicious = '<script>alert(1)</script> (2021)';
  const state = run([{ kind: "step", step: "searching", detail: malicious }]);
  assert.equal(state.entries[0].label, `Searching: ${malicious}`);
});
