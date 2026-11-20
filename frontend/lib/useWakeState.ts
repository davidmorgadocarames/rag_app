"use client";

// T13.9.1 (DEBT-056): the small bit of local state every page wires to lib/wakeUp.ts's
// onWaking/onRecovered(/onWakeFailed) callbacks to drive the shared <WakeBanner> -- kept here,
// once, rather than copied into LoginPanel/ChatPage/AdminDashboard separately.

import { useCallback, useState } from "react";

import type { WakeState } from "@/components/WakeBanner";

export interface WakeStateHandle {
  state: WakeState;
  onWaking: () => void;
  onRecovered: () => void;
  onFailed: () => void;
  reset: () => void;
}

export function useWakeState(): WakeStateHandle {
  const [state, setState] = useState<WakeState>("idle");
  const onWaking = useCallback(() => setState("waking"), []);
  const onRecovered = useCallback(() => setState("idle"), []);
  const onFailed = useCallback(() => setState("failed"), []);
  const reset = useCallback(() => setState("idle"), []);
  return { state, onWaking, onRecovered, onFailed, reset };
}
