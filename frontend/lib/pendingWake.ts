// T13.11.5 (13-J2, DEBT-060): a purely VISUAL "this call has been pending a while" signal,
// layered under (never instead of) the 13-H retry-driven wake state in `lib/wakeUp.ts`.
//
// The gap this closes: `lib/wakeUp.ts`'s retry loop only reacts to a FAILED attempt (the BFF's
// own 502/503/504, or its `fetch` throwing outright) -- it has no opinion at all while a call
// is simply still awaiting a response. On Azure, a scale-from-zero does not always fail: the
// Container Apps activator HOLDS the request (no byte sent back at all) until the replica is
// ready, so the BFF's `fetch` just sits there, unresolved, for up to the cold-start duration --
// no error is ever thrown, so `withWakeRetry` never fires `onWaking`, and the UI shows nothing
// but the button's own busy "…" for up to a minute. Proven locally (orchestrator, 2026-11-21):
// a backend that refuses the connection shows the banner after 3 s; a backend that HOLDS the
// request for 20 s shows nothing at all.
//
// Fix: a one-shot timer, armed when a BFF call starts, that flips a boolean after
// `PENDING_WAKE_DELAY_MS` if the call is STILL pending -- nothing else. No retry, no resend: the
// caller's own `start()`/`clear()` calls bracket the exact same call (or, for a stream, the
// exact same wait for the first byte/token) it would have been making anyway; this module never
// touches the network. `clear()` cancels the timer outright if it has not fired yet (the normal,
// fast-response case: the banner never appears at all), or signals "no longer pending" if it
// already had (so the caller can hide the banner the instant the response/first byte/error
// arrives).
//
// Plain TypeScript, no DOM/React imports (injectable setTimeout/clearTimeout), so `node --test`
// exercises the real logic without any real delay.

export const PENDING_WAKE_DELAY_MS = 3_000;

/** Mirrors `components/WakeBanner.tsx`'s own `WakeState` without importing a `.tsx` module
 * into a plain, DOM-light file `node --test` loads directly. */
export type PendingWakeState = "idle" | "waking" | "failed";

export interface PendingWakeTimer {
  /** Arms (or re-arms) the one-shot timer for one new logical call. */
  start: () => void;
  /**
   * Cancels the timer if it has not fired yet (nothing happens: the call was fast). If it HAD
   * already fired, calls `onCleared` exactly once (the caller hides the banner).
   */
  clear: () => void;
  /**
   * Same as `clear()`, but silent: never calls `onCleared`. For a component's unmount cleanup
   * only -- firing `onCleared` there would be a `setState` after unmount.
   */
  dispose: () => void;
}

export interface PendingWakeTimerOptions {
  /** Called at most once per `start()`, only if the call is still pending `delayMs` later. */
  onPending: () => void;
  /** Called from `clear()`, but ONLY when `onPending` had already fired for this `start()`. */
  onCleared?: () => void;
  delayMs?: number;
  setTimeoutFn?: (cb: () => void, ms: number) => unknown;
  clearTimeoutFn?: (id: unknown) => void;
}

export function createPendingWakeTimer(opts: PendingWakeTimerOptions): PendingWakeTimer {
  const delayMs = opts.delayMs ?? PENDING_WAKE_DELAY_MS;
  // Wrapped (never the raw global functions assigned directly): `setTimeout`/`clearTimeout`'s
  // own overloads don't unify with this module's intentionally untyped `unknown` timer handle
  // (the whole point of the injectable seam is to accept ANY timer implementation, real or
  // fake, not just Node/DOM's own `Timeout`/`number`).
  const setTimeoutFn: (cb: () => void, ms: number) => unknown =
    opts.setTimeoutFn ?? ((cb, ms) => setTimeout(cb, ms));
  const clearTimeoutFn: (id: unknown) => void =
    opts.clearTimeoutFn ?? ((id) => clearTimeout(id as Parameters<typeof clearTimeout>[0]));
  let timeoutId: unknown = null;
  let fired = false;

  function cancelTimeout(): void {
    if (timeoutId !== null) {
      clearTimeoutFn(timeoutId);
      timeoutId = null;
    }
  }

  function start(): void {
    cancelTimeout();
    fired = false;
    timeoutId = setTimeoutFn(() => {
      timeoutId = null;
      fired = true;
      opts.onPending();
    }, delayMs);
  }

  function clear(): void {
    cancelTimeout();
    if (fired) {
      fired = false;
      opts.onCleared?.();
    }
  }

  function dispose(): void {
    cancelTimeout();
    fired = false;
  }

  return { start, clear, dispose };
}

/**
 * Combines the 13-H retry-driven state with the purely-visual pending flag into the single
 * state `<WakeBanner>` renders. The retry state ALWAYS wins: a real 502-shaped retry already
 * "waking" (or a given-up "failed") is never masked, and never doubled, by this visual-only
 * timer -- there is exactly one banner on screen, ever (T13.11.6's "no double banner" case).
 */
export function combinePendingWakeState(
  retryState: PendingWakeState,
  pendingVisual: boolean,
): PendingWakeState {
  if (retryState !== "idle") return retryState;
  return pendingVisual ? "waking" : "idle";
}
