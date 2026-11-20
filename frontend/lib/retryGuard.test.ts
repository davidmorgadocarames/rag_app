import assert from "node:assert/strict";
import { test } from "node:test";

import { createRetryGuard } from "./retryGuard.ts";

test("createRetryGuard: a second call made before the first settles is a silent no-op (DA-13-H-1)", async () => {
  const guard = createRetryGuard();
  let calls = 0;
  let resolveFirst: (() => void) | undefined;

  // Mirrors a double-click: both calls fire before either has resolved.
  const first = guard.run(
    () =>
      new Promise<void>((resolve) => {
        calls += 1;
        resolveFirst = resolve;
      }),
  );
  // No `await` between the two -- the second call lands while the first is still in flight,
  // the exact "same tick" race the finding describes.
  const second = guard.run(async () => {
    calls += 1;
  });

  assert.equal(guard.inFlight(), true);
  assert.equal(calls, 1); // the second call never even invoked fn

  resolveFirst?.();
  await Promise.all([first, second]);

  assert.equal(calls, 1);
  assert.equal(guard.inFlight(), false);
});

test("createRetryGuard: releases after success, so a later retry can run again", async () => {
  const guard = createRetryGuard();
  let calls = 0;

  await guard.run(async () => {
    calls += 1;
  });
  assert.equal(guard.inFlight(), false);

  await guard.run(async () => {
    calls += 1;
  });

  assert.equal(calls, 2);
});

test("createRetryGuard: releases even when fn throws, so a failed retry can be retried again", async () => {
  const guard = createRetryGuard();

  await assert.rejects(
    guard.run(async () => {
      throw new Error("boom");
    }),
  );
  assert.equal(guard.inFlight(), false);

  let ran = false;
  await guard.run(async () => {
    ran = true;
  });
  assert.equal(ran, true);
});
