// T14.4.1/T14.4.2 (block 14-D2): the chat UI's visible "plan" panel -- a pure reducer over
// the backend's own ``StreamStep`` SSE events (lib/chatStream.ts), same split as
// lib/wakeSteps.ts/useWakeSteps.ts (reducer here, React wiring in usePlanSteps.ts, UI in
// components/PlanPanel.tsx).
//
// Backend sequence this mirrors (rag_app.generation.answer_question_stream, T14.4.1):
//   step(understood, end)               -- EVERY turn, incl. chitchat/ambiguous/off_topic
//   [security/followup only:]
//   step(searching, start) -> step(found_sources, end) -> step(writing_answer, start)
//
// Unlike wakeSteps.ts's FIXED small set of rows (always shown, pending/current/done), this
// panel is a growing TRANSCRIPT: each new recognised step APPENDS one entry; there is no
// "pending" row for a step that has not happened yet (a turn that never retrieves simply
// never gets a "Searching"/"Found"/"Writing" entry at all). An unrecognised `step` name
// (e.g. the pre-existing `embed_query`, which this panel does not show) is a forward-
// compatible no-op, same convention `reduceWakeStep` already uses for names it does not
// know either.
//
// Plain TypeScript, no DOM/React imports -- `node --test` exercises this reducer directly.

export type PlanStepId = "understood" | "searching" | "found_sources" | "writing_answer";

const PLAN_STEP_IDS: ReadonlySet<string> = new Set([
  "understood",
  "searching",
  "found_sources",
  "writing_answer",
]);

export interface PlanEntry {
  id: PlanStepId;
  /** Already the final, human-readable text -- e.g. "Understood: security/en" -- built from
   * the step id + the server's `detail` (never raw/unescaped HTML; `PlanPanel.tsx` renders
   * it as a plain text node). */
  label: string;
}

export interface PlanStepsState {
  entries: readonly PlanEntry[];
}

export function initialPlanStepsState(): PlanStepsState {
  return { entries: [] };
}

function labelFor(id: PlanStepId, detail: string | undefined): string {
  switch (id) {
    case "understood":
      return `Understood: ${detail ?? ""}`;
    case "searching":
      return `Searching: ${detail ?? ""}`;
    case "found_sources":
      return `Found ${detail ?? "0"} sources`;
    case "writing_answer":
      return "Writing answer";
  }
}

export type PlanStepEvent =
  | { kind: "step"; step: string; detail?: string }
  | { kind: "reset" };

export function reducePlanStep(state: PlanStepsState, event: PlanStepEvent): PlanStepsState {
  if (event.kind === "reset") return initialPlanStepsState();
  if (!PLAN_STEP_IDS.has(event.step)) return state; // forward-compat no-op (e.g. embed_query)
  const id = event.step as PlanStepId;
  const entry: PlanEntry = { id, label: labelFor(id, event.detail) };
  return { entries: [...state.entries, entry] };
}
