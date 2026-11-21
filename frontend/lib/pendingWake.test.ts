import assert from "node:assert/strict";
import { test } from "node:test";

import { combinePendingWakeState, createPendingWakeTimer } from "./pendingWake.ts";

// A tiny deterministic fake of setTimeout/clearTimeout: `start()`/`clear()` run synchronously
// in these tests, and `fire()` runs every still-armed callback whose own delay has "elapsed" --
// no real waiting, ever.
function fakeTimers() {
  let nextId = 1;
  const pending = new Map<number, () => void>();
  return {
    setTimeoutFn: (cb: () => void) => {
      const id = nextId++;
      pending.set(id, cb);
      return id;
    },
    clearTimeoutFn: (id: unknown) => {
      pending.delete(id as number);
    },
    /** Runs every callback still armed right now, as if their delay had elapsed. */
    fire: () => {
      const callbacks = [...pending.values()];
      pending.clear();
      for (const cb of callbacks) cb();
    },
    armedCount: () => pending.size,
  };
}

test("createPendingWakeTimer: never fires before clear() cancels it (fast call, negative control)", () => {
  const timers = fakeTimers();
  let pendingCalls = 0;
  const timer = createPendingWakeTimer({
    onPending: () => {
      pendingCalls += 1;
    },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  assert.equal(timers.armedCount(), 1);
  timer.clear(); // the response arrived before the threshold
  assert.equal(timers.armedCount(), 0);
  timers.fire(); // nothing left to fire -- but even if something were, assert below covers it
  assert.equal(pendingCalls, 0, "onPending must never fire once clear() cancelled it in time");
});

test("createPendingWakeTimer: fires onPending exactly once once the delay elapses", () => {
  const timers = fakeTimers();
  let pendingCalls = 0;
  const timer = createPendingWakeTimer({
    onPending: () => {
      pendingCalls += 1;
    },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  timers.fire();
  assert.equal(pendingCalls, 1);
  // Firing again (e.g. a stray second tick) must not call onPending a second time.
  timers.fire();
  assert.equal(pendingCalls, 1);
});

test("createPendingWakeTimer: clear() after it fired calls onCleared exactly once", () => {
  const timers = fakeTimers();
  let pendingCalls = 0;
  let clearedCalls = 0;
  const timer = createPendingWakeTimer({
    onPending: () => {
      pendingCalls += 1;
    },
    onCleared: () => {
      clearedCalls += 1;
    },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  timers.fire();
  assert.equal(pendingCalls, 1);
  timer.clear();
  assert.equal(clearedCalls, 1);
  // Clearing again (e.g. a defensive second call from onDone AND onError) is a no-op.
  timer.clear();
  assert.equal(clearedCalls, 1);
});

test("createPendingWakeTimer: clear() before it fired never calls onCleared (nothing to hide)", () => {
  const timers = fakeTimers();
  let clearedCalls = 0;
  const timer = createPendingWakeTimer({
    onPending: () => undefined,
    onCleared: () => {
      clearedCalls += 1;
    },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  timer.clear();
  assert.equal(clearedCalls, 0);
});

test("createPendingWakeTimer: dispose() after it fired is silent (component-unmount cleanup, no setState)", () => {
  const timers = fakeTimers();
  let pendingCalls = 0;
  let clearedCalls = 0;
  const timer = createPendingWakeTimer({
    onPending: () => {
      pendingCalls += 1;
    },
    onCleared: () => {
      clearedCalls += 1;
    },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  timers.fire();
  assert.equal(pendingCalls, 1);
  timer.dispose();
  assert.equal(clearedCalls, 0, "dispose() must never call onCleared -- no state update after unmount");
});

test("createPendingWakeTimer: dispose() before it fired cancels the still-armed timeout", () => {
  const timers = fakeTimers();
  const timer = createPendingWakeTimer({
    onPending: () => undefined,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  assert.equal(timers.armedCount(), 1);
  timer.dispose();
  assert.equal(timers.armedCount(), 0);
});

test("createPendingWakeTimer: start() re-arms cleanly for a second logical call", () => {
  const timers = fakeTimers();
  let pendingCalls = 0;
  const timer = createPendingWakeTimer({
    onPending: () => {
      pendingCalls += 1;
    },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  timer.start();
  timers.fire();
  assert.equal(pendingCalls, 1);
  timer.clear();

  // A brand new call: must be able to show the banner again on its own merits.
  timer.start();
  timers.fire();
  assert.equal(pendingCalls, 2);
});

test("createPendingWakeTimer: the default delay is exactly PENDING_WAKE_DELAY_MS", () => {
  let capturedDelay: number | undefined;
  const timer = createPendingWakeTimer({
    onPending: () => undefined,
    setTimeoutFn: (cb, ms) => {
      capturedDelay = ms;
      return 1;
    },
    clearTimeoutFn: () => undefined,
  });
  timer.start();
  assert.equal(capturedDelay, 3_000);
});

// --- combinePendingWakeState ---------------------------------------------------------------

test("combinePendingWakeState: idle retry state + no pending visual -> idle", () => {
  assert.equal(combinePendingWakeState("idle", false), "idle");
});

test("combinePendingWakeState: idle retry state + pending visual -> waking (the new visual path)", () => {
  assert.equal(combinePendingWakeState("idle", true), "waking");
});

test("combinePendingWakeState: a real retry already 'waking' wins -- never a second/different banner", () => {
  assert.equal(combinePendingWakeState("waking", true), "waking");
  assert.equal(combinePendingWakeState("waking", false), "waking");
});

test("combinePendingWakeState: a given-up retry ('failed') is never downgraded back to 'waking' by the visual timer", () => {
  assert.equal(combinePendingWakeState("failed", true), "failed");
  assert.equal(combinePendingWakeState("failed", false), "failed");
});
