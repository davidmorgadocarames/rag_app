// T13.12.6 (DEBT-062): the REAL pipeline sub-steps shown inside the chat wake banner, once a
// cold start is confirmed still pending (lib/pendingWake.ts's own ~3 s threshold). A pure
// reducer over the backend's own stage/step SSE events (lib/chatStream.ts) -- never a timing
// guess: a step only becomes "current" when its own real SSE event arrives, and every
// earlier step only becomes "done" once a LATER real event supersedes it.
//
// Backend sequence this mirrors (rag_app.generation.answer_question_stream, T13.12.5):
//   stage=classifying -> [chitchat: stage=responding -> tokens]
//                      -> [security: stage=retrieving -> step(embed_query,start)
//                          -> step(embed_query,end) -> [stage=reranking] -> stage=generating
//                          -> stage=checking -> tokens/done]
// The UI box lists ONE extra row ("embed") between "backend" and "retrieving" that the
// backend itself does not have as its own top-level stage (it is backend's
// retrieving/embed_query atomic step) -- the dominant cold-start cost (Ollama waking
// bge-m3), surfaced on its own line because it is the one users actually wait on.
//
// Plain TypeScript, no DOM/React imports -- `node --test` exercises this reducer directly;
// lib/useWakeSteps.ts wraps it with React state + a 1s elapsed-seconds ticker for display.

export type WakeStepId = "backend" | "embed" | "retrieving" | "reranking" | "generating" | "checking";

export type WakeStepStatus = "pending" | "current" | "done";

export const WAKE_STEP_ORDER: readonly WakeStepId[] = [
  "backend",
  "embed",
  "retrieving",
  "reranking",
  "generating",
  "checking",
];

export const WAKE_STEP_LABELS: Readonly<Record<WakeStepId, string>> = {
  backend: "Backend starting (container + reranker model)",
  embed: "Waking the embeddings model (Ollama bge-m3)",
  retrieving: "Retrieving",
  reranking: "Reranking",
  generating: "Generating",
  checking: "Checking",
};

export interface WakeStepsState {
  statuses: Readonly<Record<WakeStepId, WakeStepStatus>>;
  /** The one step currently in progress, or `null` right after the pipeline retired
   * "backend" but before its next concrete event (e.g. between "conversation" and the
   * first stage event) -- nothing is highlighted as current for that brief instant. */
  current: WakeStepId | null;
  /** ms epoch the CURRENT step began (null iff `current` is null); the caller's own
   * elapsed-seconds display only -- never read by the reducer itself. */
  currentSince: number | null;
}

/** One real SSE event the reducer reacts to -- a small, stable shape kept separate from
 * `chatStream.ts`'s own `StreamEvent` (no DOM/fetch import) so this module stays plain
 * TypeScript, testable with `node --test`. */
export type WakeStepEvent =
  | { kind: "stage"; stage: string }
  | { kind: "step"; step: string; phase: "start" | "end" }
  // conversation / token / done / error / any future event type this reducer does not
  // otherwise react to -- still counts as "the pipeline produced ITS FIRST byte", which is
  // what retires the "backend" row.
  | { kind: "other" };

export function initialWakeStepsState(now: number): WakeStepsState {
  const statuses = Object.fromEntries(
    WAKE_STEP_ORDER.map((id) => [id, id === "backend" ? "current" : "pending"]),
  ) as Record<WakeStepId, WakeStepStatus>;
  return { statuses, current: "backend", currentSince: now };
}

function furthestIndex(state: WakeStepsState): number {
  let furthest = -1;
  WAKE_STEP_ORDER.forEach((id, i) => {
    if (state.statuses[id] !== "pending") furthest = Math.max(furthest, i);
  });
  return furthest;
}

/** Marks every step strictly before `id` done and `id` itself current. A no-op if `id` is
 * not AHEAD of the furthest step already reached (defends against a stale/out-of-order
 * replay of an earlier event -- this pipeline only ever moves forward). */
function moveTo(state: WakeStepsState, id: WakeStepId, now: number): WakeStepsState {
  const idx = WAKE_STEP_ORDER.indexOf(id);
  if (idx <= furthestIndex(state)) return state;
  const statuses = { ...state.statuses } as Record<WakeStepId, WakeStepStatus>;
  for (let i = 0; i < idx; i++) statuses[WAKE_STEP_ORDER[i]] = "done";
  statuses[id] = "current";
  return { statuses, current: id, currentSince: now };
}

/** Retires "backend" the instant ANY event arrives, with nothing yet highlighted as the
 * new current step (a no-op once backend is already done). */
function retireBackend(state: WakeStepsState): WakeStepsState {
  if (state.statuses.backend !== "pending" && state.statuses.backend !== "current") return state;
  return { statuses: { ...state.statuses, backend: "done" }, current: null, currentSince: null };
}

export function reduceWakeStep(state: WakeStepsState, event: WakeStepEvent, now: number): WakeStepsState {
  if (event.kind === "other") return retireBackend(state);
  if (event.kind === "stage") {
    if (event.stage === "reranking") return moveTo(state, "reranking", now);
    if (event.stage === "generating" || event.stage === "responding") {
      return moveTo(state, "generating", now);
    }
    if (event.stage === "checking") return moveTo(state, "checking", now);
    // "classifying"/"retrieving" (and any future stage name): no dedicated box row of its
    // own -- it still counts as "the pipeline is alive", same as `retireBackend`.
    return retireBackend(state);
  }
  // event.kind === "step"
  if (event.step !== "embed_query") return retireBackend(state); // unknown step: forward-compat no-op
  return moveTo(state, event.phase === "start" ? "embed" : "retrieving", now);
}
