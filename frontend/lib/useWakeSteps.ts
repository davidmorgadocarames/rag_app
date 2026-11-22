"use client";

// T13.12.6 (DEBT-062): the React wiring for lib/wakeSteps.ts's pure reducer -- CHAT ONLY
// (never wired into LoginPanel/AdminDashboard, which keep the plain <WakeBanner> with no
// steps). Owns the 1s elapsed-seconds ticker for the CURRENT step's display -- kept OUT of
// the pure reducer (which never reads the clock itself) and out of the aria-live region
// (see WakeBanner.tsx: the ticking text is `aria-hidden`, only a step CHANGE is announced,
// never a per-second tick -- T13.12.6's own "no announcement spam" requirement).

import { useCallback, useEffect, useState } from "react";

import {
  type WakeStepEvent,
  type WakeStepsState,
  initialWakeStepsState,
  reduceWakeStep,
} from "./wakeSteps";

export interface WakeStepsHandle {
  state: WakeStepsState;
  /** Seconds since the current step became current (0 while nothing is current). */
  elapsedSeconds: number;
  onStage: (stage: string) => void;
  onStep: (stage: string, step: string, phase: "start" | "end") => void;
  /** Any other stream event (conversation/token/done/error) -- see wakeSteps.ts's "other". */
  onOther: () => void;
  /** Re-arms the state machine for one new logical turn. */
  reset: () => void;
}

export function useWakeSteps(): WakeStepsHandle {
  const [state, setState] = useState<WakeStepsState>(() => initialWakeStepsState(Date.now()));
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  const dispatch = useCallback((event: WakeStepEvent) => {
    setState((prev) => reduceWakeStep(prev, event, Date.now()));
  }, []);

  const onStage = useCallback((stage: string) => dispatch({ kind: "stage", stage }), [dispatch]);
  const onStep = useCallback(
    (_stage: string, step: string, phase: "start" | "end") => dispatch({ kind: "step", step, phase }),
    [dispatch],
  );
  const onOther = useCallback(() => dispatch({ kind: "other" }), [dispatch]);
  const reset = useCallback(() => {
    setState(initialWakeStepsState(Date.now()));
    setElapsedSeconds(0);
  }, []);

  // Ticks ONLY while a step is current (no interval at all once the turn is done/idle) --
  // re-armed whenever `currentSince` changes (a new step became current).
  const currentSince = state.currentSince;
  useEffect(() => {
    if (currentSince === null) {
      setElapsedSeconds(0);
      return;
    }
    setElapsedSeconds(Math.floor((Date.now() - currentSince) / 1000));
    const id = setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - currentSince) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [currentSince]);

  return { state, elapsedSeconds, onStage, onStep, onOther, reset };
}
