// DA-13-H-1 fix (13-H-fix): a synchronous in-flight guard for a `<WakeBanner>` Retry handler.
//
// The bug this closes: `onRetryWake()`/`submitLogin()`/`refreshAll()` are only ever gated by
// React state (`busy`), which does not change synchronously -- two clicks (or two keyboard
// activations) landing in the SAME tick, before React has re-rendered away from the previous
// state, both read the stale `busy === false` and both fire the underlying call. For the chat
// Retry button that means the SAME question is sent to the backend twice: a duplicate answer
// and a double daily-cap charge, exactly the scenario this block's own Done-when rules out.
//
// `createRetryGuard()` is a plain closure variable (not React state): it flips to `true` the
// instant `run()` is entered, synchronously, before any `await` -- so a second call made in the
// same tick (or any time before the first one settles) always observes `true` and is a silent
// no-op, the same semantics as the primary Send/ENTER button's own `if (busy) return`. One
// guard per page (ChatPage, LoginPanel, AdminDashboard) instead of copying the pattern three
// times with the risk of getting the ordering wrong in one of them.
//
// Plain TypeScript, no DOM/React imports, so `node --test` exercises the real logic.

export interface RetryGuard {
  /** True while a previous `run()` call has not yet settled. */
  inFlight: () => boolean;
  /**
   * Runs `fn` unless a previous call is still in flight, in which case this is a no-op. The
   * in-flight flag is set synchronously before `fn` is even invoked, and cleared in a `finally`
   * so a later retry (after success OR failure) can run again.
   */
  run: (fn: () => Promise<void>) => Promise<void>;
}

export function createRetryGuard(): RetryGuard {
  let inFlight = false;
  return {
    inFlight: () => inFlight,
    async run(fn: () => Promise<void>): Promise<void> {
      if (inFlight) return;
      inFlight = true;
      try {
        await fn();
      } finally {
        inFlight = false;
      }
    },
  };
}
