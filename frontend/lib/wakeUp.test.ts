import assert from "node:assert/strict";
import { test } from "node:test";

import { backoffDelayMs, classifyWakeSignal, WakeUpError, withWakeRetry } from "./wakeUp.ts";

// --- classifyWakeSignal (pure) -----------------------------------------------------------

test("classifyWakeSignal: a real 4xx/other 5xx is never a wake-up signal", () => {
  assert.equal(classifyWakeSignal(401, "unauthorized"), "none");
  assert.equal(classifyWakeSignal(403, "forbidden"), "none");
  assert.equal(classifyWakeSignal(404, "not found"), "none");
  assert.equal(classifyWakeSignal(422, "validation"), "none");
  assert.equal(classifyWakeSignal(429, "rate limited"), "none");
  assert.equal(classifyWakeSignal(500, "internal error"), "none");
  assert.equal(classifyWakeSignal(200, null), "none");
});

test("classifyWakeSignal: a connect-level failure (no response at all) is always safe", () => {
  assert.equal(classifyWakeSignal(null, null), "safe");
});

test("classifyWakeSignal: the BFF's own 'upstream unreachable' 502 is safe (never reached the backend)", () => {
  assert.equal(classifyWakeSignal(502, "upstream unreachable"), "safe");
});

test("classifyWakeSignal: a BFF timeout or any other 502/503/504 pass-through is unsafe", () => {
  assert.equal(classifyWakeSignal(502, "upstream timed out"), "unsafe");
  assert.equal(classifyWakeSignal(503, "service unavailable"), "unsafe");
  assert.equal(classifyWakeSignal(504, "gateway timeout"), "unsafe");
  assert.equal(classifyWakeSignal(502, null), "unsafe");
});

// --- backoffDelayMs (pure) --------------------------------------------------------------

test("backoffDelayMs grows then caps, with a fixed rand it is deterministic", () => {
  const rand = () => 0.5; // no jitter offset
  const d1 = backoffDelayMs(1, rand);
  const d2 = backoffDelayMs(2, rand);
  const d10 = backoffDelayMs(10, rand);
  assert.ok(d1 >= 900 && d1 <= 1100, `d1=${d1}`);
  assert.ok(d2 > d1, `d2=${d2} should exceed d1=${d1}`);
  assert.ok(d10 <= 8000, `d10=${d10} should be capped at 8000`);
});

test("backoffDelayMs jitter stays within +/-20%", () => {
  const low = backoffDelayMs(1, () => 0); // -20%
  const high = backoffDelayMs(1, () => 1); // +20%
  assert.ok(low < 1000 && low >= 790, `low=${low}`);
  assert.ok(high > 1000 && high <= 1210, `high=${high}`);
});

// --- withWakeRetry -----------------------------------------------------------------------

function fakeClock() {
  let t = 0;
  return {
    now: () => t,
    sleep: async (ms: number) => {
      t += ms;
    },
  };
}

test("withWakeRetry: a real error (classified 'none') is rethrown immediately, no retry/backoff", async () => {
  let calls = 0;
  const { now, sleep } = fakeClock();
  await assert.rejects(
    () =>
      withWakeRetry({
        attempt: async () => {
          calls += 1;
          throw new Error("wrong password");
        },
        classifyError: () => "none",
        idempotent: false,
        now,
        sleep,
      }),
    /wrong password/,
  );
  assert.equal(calls, 1);
});

test("withWakeRetry: a 'safe' failure is retried automatically until it succeeds, with onWaking/onRecovered fired", async () => {
  let calls = 0;
  let waking = 0;
  let recovered = 0;
  const { now, sleep } = fakeClock();
  const result = await withWakeRetry({
    attempt: async () => {
      calls += 1;
      if (calls < 3) throw new Error("connect failed");
      return "ok";
    },
    classifyError: () => "safe",
    idempotent: false,
    onWaking: () => {
      waking += 1;
    },
    onRecovered: () => {
      recovered += 1;
    },
    now,
    sleep,
    rand: () => 0.5,
  });
  assert.equal(result, "ok");
  assert.equal(calls, 3);
  assert.equal(waking, 1); // only the FIRST failure flips into the waking state
  assert.equal(recovered, 1);
});

test("withWakeRetry: a 'safe' failure that never resolves is exhausted at the budget, as a WakeUpError", async () => {
  let calls = 0;
  const { now, sleep } = fakeClock();
  await assert.rejects(
    () =>
      withWakeRetry({
        attempt: async () => {
          calls += 1;
          throw new Error("still cold");
        },
        classifyError: () => "safe",
        idempotent: false,
        budgetMs: 5_000,
        now,
        sleep,
        rand: () => 0.5,
      }),
    (err: unknown) => err instanceof WakeUpError && err.phase === "exhausted",
  );
  assert.ok(calls > 1, "should have retried at least once before giving up");
});

test("withWakeRetry: an 'unsafe' failure on a NON-idempotent call throws immediately -- no automatic resend", async () => {
  let calls = 0;
  let waking = 0;
  const { now, sleep } = fakeClock();
  await assert.rejects(
    () =>
      withWakeRetry({
        attempt: async () => {
          calls += 1;
          throw new Error("timed out");
        },
        classifyError: () => "unsafe",
        idempotent: false,
        onWaking: () => {
          waking += 1;
        },
        now,
        sleep,
      }),
    (err: unknown) => err instanceof WakeUpError && err.phase === "unsafe",
  );
  // Exactly one attempt: a chat question (or any other non-idempotent action) is NEVER
  // resubmitted automatically once the BFF cannot prove it never reached the backend.
  assert.equal(calls, 1);
  assert.equal(waking, 1, "the waking state is still shown, just without an auto-retry loop");
});

test("withWakeRetry: an 'unsafe' failure on an idempotent call IS retried, like 'safe'", async () => {
  let calls = 0;
  const { now, sleep } = fakeClock();
  const result = await withWakeRetry({
    attempt: async () => {
      calls += 1;
      if (calls < 2) throw new Error("503 service unavailable");
      return "ok";
    },
    classifyError: () => "unsafe",
    idempotent: true,
    now,
    sleep,
    rand: () => 0.5,
  });
  assert.equal(result, "ok");
  assert.equal(calls, 2);
});
