"use client";

// T14.4.2 (block 14-D2): the React wiring for lib/planSteps.ts's pure reducer -- CHAT ONLY,
// same split as lib/useWakeSteps.ts/lib/wakeSteps.ts. Owns no ticking clock (unlike
// useWakeSteps.ts) -- the plan panel has no elapsed-seconds display, only the growing
// transcript of steps the server has actually reported.

import { useCallback, useState } from "react";

import {
  type PlanStepsState,
  initialPlanStepsState,
  reducePlanStep,
} from "./planSteps";

export interface PlanStepsHandle {
  state: PlanStepsState;
  onStep: (step: string, detail?: string) => void;
  /** Re-arms the state machine for one new logical turn. */
  reset: () => void;
}

export function usePlanSteps(): PlanStepsHandle {
  const [state, setState] = useState<PlanStepsState>(() => initialPlanStepsState());

  const onStep = useCallback((step: string, detail?: string) => {
    setState((prev) => reducePlanStep(prev, { kind: "step", step, detail }));
  }, []);
  const reset = useCallback(() => {
    setState(initialPlanStepsState());
  }, []);

  return { state, onStep, reset };
}
