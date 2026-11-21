"use client";

// T13.9.1 (DEBT-056): the small bit of local state every page wires to lib/wakeUp.ts's
// onWaking/onRecovered(/onWakeFailed) callbacks to drive the shared <WakeBanner> -- kept here,
// once, rather than copied into LoginPanel/ChatPage/AdminDashboard separately.
//
// T13.11.5 (13-J2, DEBT-060): ALSO owns the purely-visual "still pending after ~3 s" timer
// (lib/pendingWake.ts) -- a SEPARATE signal from the retry state above, combined into the one
// `state` <WakeBanner> renders via `combinePendingWakeState` (the retry state always wins, so
// the two mechanisms never show two different banners at once). `startPending`/`clearPending`
// are the two calls every page brackets around a BFF call (or, for a stream, around the wait
// for its first byte/token) with -- no retry, no resend, exactly the existing 13-H policy.

import { useCallback, useEffect, useRef, useState } from "react";

import type { WakeState } from "@/components/WakeBanner";
import { combinePendingWakeState, createPendingWakeTimer } from "./pendingWake";

export interface WakeStateHandle {
  state: WakeState;
  onWaking: () => void;
  onRecovered: () => void;
  onFailed: () => void;
  reset: () => void;
  /** Arms the ~3 s visual timer for one new logical BFF call. */
  startPending: () => void;
  /** Call the instant the response (or, for a stream, its first byte/token) arrives or fails. */
  clearPending: () => void;
}

export function useWakeState(): WakeStateHandle {
  const [retryState, setRetryState] = useState<WakeState>("idle");
  const [pendingVisual, setPendingVisual] = useState(false);

  const onWaking = useCallback(() => setRetryState("waking"), []);
  const onRecovered = useCallback(() => setRetryState("idle"), []);
  const onFailed = useCallback(() => setRetryState("failed"), []);
  const reset = useCallback(() => setRetryState("idle"), []);

  // One timer instance for the lifetime of the component (a ref, not state -- its own
  // start()/clear()/dispose() calls are what drive `pendingVisual`, never the other way
  // round).
  const timerRef = useRef(
    createPendingWakeTimer({
      onPending: () => setPendingVisual(true),
      onCleared: () => setPendingVisual(false),
    }),
  );

  // Unmount cleanup: `dispose()`, never `clear()` -- a still-armed timer must be cancelled
  // WITHOUT calling `onCleared` (which would be a setState after unmount).
  useEffect(() => {
    const timer = timerRef.current;
    return () => timer.dispose();
  }, []);

  const startPending = useCallback(() => timerRef.current.start(), []);
  const clearPending = useCallback(() => timerRef.current.clear(), []);

  const state = combinePendingWakeState(retryState, pendingVisual);

  return { state, onWaking, onRecovered, onFailed, reset, startPending, clearPending };
}
