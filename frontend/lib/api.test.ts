import assert from "node:assert/strict";
import { test } from "node:test";

import {
  adminFeatures,
  ApiError,
  confirmPasswordReset,
  CSRF_COOKIE_NAME,
  deleteAccount,
  login,
  logout,
  me,
  readCookie,
  register,
  resendVerification,
  streamChat,
  verifyEmail,
} from "./api.ts";
import { WakeUpError } from "./wakeUp.ts";

// --- readCookie (pure) ----------------------------------------------------------------

test("readCookie finds the named cookie among several, and nothing when absent", () => {
  assert.equal(readCookie("", CSRF_COOKIE_NAME), null);
  assert.equal(readCookie("other=1", CSRF_COOKIE_NAME), null);
  assert.equal(readCookie(`${CSRF_COOKIE_NAME}=abc123`, CSRF_COOKIE_NAME), "abc123");
  assert.equal(
    readCookie(`other=1; ${CSRF_COOKIE_NAME}=abc123; third=2`, CSRF_COOKIE_NAME),
    "abc123",
  );
  // leading/trailing whitespace around the pair is trimmed
  assert.equal(readCookie(`other=1;  ${CSRF_COOKIE_NAME}=abc123 ; third=2`, CSRF_COOKIE_NAME), "abc123");
});

// --- request() / the BFF contract (T12.1.7) --------------------------------------------

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(calls: Call[], makeResponse: () => Response): typeof fetch {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return makeResponse(); // a fresh Response per call: a body can only be read once
  }) as typeof fetch;
}

function withDocumentCookie<T>(cookie: string, fn: () => Promise<T>): Promise<T> {
  const g = globalThis as { document?: { cookie: string } };
  const previous = g.document;
  g.document = { cookie };
  return fn().finally(() => {
    g.document = previous;
  });
}

test("login calls the relative BFF path, with same-origin credentials and a CSRF header read from the cookie", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(
    calls,
    () => new Response(JSON.stringify({ token_type: "bearer" }), { status: 200 }),
  );
  try {
    await withDocumentCookie(`${CSRF_COOKIE_NAME}=the-csrf-token`, () =>
      login("alice@example.test", "hunter2x"),
    );
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls.length, 1);
  // Relative, BFF-only path -- never an absolute backend URL (old code used NEXT_PUBLIC_API_URL).
  assert.equal(calls[0].url, "/api/auth/login");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.credentials, "same-origin");
  const headers = calls[0].init.headers as Record<string, string>;
  assert.equal(headers["X-CSRF-Token"], "the-csrf-token");
  // Never a Bearer header any more -- the session travels only as a cookie.
  assert.equal("Authorization" in headers, false);
});

test("a GET (me) never carries a CSRF header, even with the cookie set", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(
    calls,
    () =>
      new Response(JSON.stringify({ id: "u1", email: "a@b.test", email_verified: false }), {
        status: 200,
      }),
  );
  try {
    await withDocumentCookie(`${CSRF_COOKIE_NAME}=irrelevant`, () => me());
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls[0].url, "/api/auth/me");
  assert.equal((calls[0].init.method ?? "GET").toUpperCase(), "GET");
  const headers = calls[0].init.headers as Record<string, string>;
  assert.equal("X-CSRF-Token" in headers, false);
});

test("no CSRF cookie yet (e.g. register's very first call) sends no CSRF header", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(calls, () => new Response(JSON.stringify({}), { status: 201 }));
  try {
    await withDocumentCookie("", () => register("bob@example.test", "correcthorse"));
  } finally {
    globalThis.fetch = original;
  }
  const headers = calls[0].init.headers as Record<string, string>;
  assert.equal("X-CSRF-Token" in headers, false);
});

test("logout and deleteAccount also go through the relative BFF path", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(
    calls,
    () => new Response(JSON.stringify({ detail: "ok" }), { status: 200 }),
  );
  try {
    await logout();
    await deleteAccount();
  } finally {
    globalThis.fetch = original;
  }
  assert.deepEqual(calls.map((c) => [c.url, c.init.method]), [
    ["/api/auth/logout", "POST"],
    ["/api/account", "DELETE"],
  ]);
});

test("a non-ok response raises ApiError with the backend's detail and status", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ detail: "invalid credentials" }), { status: 401 })) as typeof fetch;
  try {
    await assert.rejects(
      () => login("alice@example.test", "wrong"),
      (err: unknown) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.status, 401);
        assert.equal(err.message, "invalid credentials");
        return true;
      },
    );
  } finally {
    globalThis.fetch = original;
  }
});

// --- T13.9.1 (DEBT-056): wake-up state -- request()'s shared retry/backoff ----------------

function fakeClock() {
  let t = 0;
  return {
    now: () => t,
    sleep: async (ms: number) => {
      t += ms;
    },
  };
}

test("login: a real 4xx (wrong password) is NEVER treated as waking up -- negative control", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    return new Response(JSON.stringify({ detail: "invalid credentials" }), { status: 401 });
  }) as typeof fetch;
  let waking = false;
  try {
    await assert.rejects(
      () => login("alice@example.test", "wrong", { onWaking: () => (waking = true) }),
      (err: unknown) => {
        assert.ok(err instanceof ApiError);
        assert.equal((err as ApiError).status, 401);
        return true;
      },
    );
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls, 1, "a real 4xx must never trigger a retry");
  assert.equal(waking, false, "a real 4xx must never show the waking-up state");
});

test("login: the BFF's 'upstream unreachable' 502 (cold start, connect refused) is retried automatically", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    if (calls < 3) {
      return new Response(JSON.stringify({ detail: "upstream unreachable" }), { status: 502 });
    }
    return new Response(JSON.stringify({ token_type: "bearer" }), { status: 200 });
  }) as typeof fetch;
  const { now, sleep } = fakeClock();
  let waking = false;
  let recovered = false;
  try {
    await login("alice@example.test", "hunter2x", {
      now,
      sleep,
      onWaking: () => (waking = true),
      onRecovered: () => (recovered = true),
    });
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls, 3, "login resumes automatically once the backend wakes up");
  assert.equal(waking, true);
  assert.equal(recovered, true);
});

test("login: a BFF timeout that never recovers gives up at the budget as a WakeUpError, not a generic ApiError", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ detail: "upstream timed out" }), { status: 502 })) as typeof fetch;
  const { now, sleep } = fakeClock();
  try {
    await assert.rejects(
      () => login("alice@example.test", "hunter2x", { now, sleep, budgetMs: 3_000 }),
      (err: unknown) => err instanceof WakeUpError && err.phase === "exhausted",
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("adminFeatures (a GET): an ambiguous 503 is retried automatically even though it is 'unsafe' -- GETs are idempotent by default", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    if (calls < 2) return new Response(JSON.stringify({ detail: "service unavailable" }), { status: 503 });
    return new Response(JSON.stringify({ features: [], live_mode: "polling", alerts: [] }), {
      status: 200,
    });
  }) as typeof fetch;
  const { now, sleep } = fakeClock();
  try {
    const result = await adminFeatures({ now, sleep });
    assert.deepEqual(result.features, []);
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls, 2);
});

function sseBody(...lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(line));
      controller.close();
    },
  });
}

test("streamChat: a real 4xx (e.g. a vanished conversation, 404) is reported via onError, never as waking up", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
  }) as typeof fetch;
  const events: string[] = [];
  let waking = false;
  try {
    await streamChat(
      { question: "hi", conversation_id: "c1" },
      {
        onWaking: () => (waking = true),
        onError: (detail, id, drop) => events.push(`error:${detail}:${id}:${drop}`),
      },
    );
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls, 1, "a real 404 must never be retried");
  assert.equal(waking, false);
  assert.deepEqual(events, ["error:This conversation no longer exists (it was deleted). Your next message starts a new conversation.:null:true"]);
});

test("streamChat: a connect-level failure (cold start, nothing reached the backend) auto-retries, then streams through", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    if (calls < 3) throw new TypeError("fetch failed"); // ECONNREFUSED-shaped, no response at all
    return new Response(sseBody('data: {"type":"token","text":"hi"}\n\n', 'data: {"type":"done"}\n\n'), {
      status: 200,
    });
  }) as typeof fetch;
  const { now, sleep } = fakeClock();
  const events: string[] = [];
  let waking = false;
  let recovered = false;
  try {
    await streamChat(
      { question: "How do I prevent SQL injection?" },
      {
        onToken: (t) => events.push(`token:${t}`),
        onDone: () => events.push("done"),
        onWaking: () => (waking = true),
        onRecovered: () => (recovered = true),
      },
      { now, sleep },
    );
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls, 3);
  assert.equal(waking, true);
  assert.equal(recovered, true);
  assert.deepEqual(events, ["token:hi", "done"]);
});

test("streamChat: a BFF timeout (502 'upstream timed out') is NEVER auto-resubmitted -- onWakeFailed fires once, not onError", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    return new Response(JSON.stringify({ detail: "upstream timed out" }), { status: 502 });
  }) as typeof fetch;
  const events: string[] = [];
  try {
    await streamChat(
      { question: "How do I prevent SQL injection?" },
      {
        onError: () => events.push("error"),
        onWaking: () => events.push("waking"),
        onWakeFailed: () => events.push("wake-failed"),
      },
    );
  } finally {
    globalThis.fetch = original;
  }
  // Exactly ONE attempt: the question may already have reached the LLM, so it is never
  // resent automatically -- the UI must offer an explicit, user-initiated Retry instead.
  assert.equal(calls, 1);
  assert.deepEqual(events, ["waking", "wake-failed"]);
});

// --- T12.4.4/T12.4.5: resend-verification (by email, no session) + verify/reset (by token) ---

test("resendVerification posts the email, not an authenticated session", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(
    calls,
    () => new Response(JSON.stringify({ detail: "ok" }), { status: 200 }),
  );
  try {
    await resendVerification("alice@example.test");
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls[0].url, "/api/auth/resend-verification");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ email: "alice@example.test" }));
});

test("verifyEmail posts the token in the body, never as a query string", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(
    calls,
    () => new Response(JSON.stringify({ detail: "Email verified. You can now log in." }), {
      status: 200,
    }),
  );
  try {
    await verifyEmail("a-real-token");
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls[0].url, "/api/auth/verify");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ token: "a-real-token" }));
});

test("confirmPasswordReset posts the token and the new password", async () => {
  const calls: Call[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = stubFetch(calls, () => new Response(JSON.stringify({}), { status: 404 }));
  try {
    await assert.rejects(() => confirmPasswordReset("a-token", "a-new-password-1234"));
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls[0].url, "/api/auth/reset");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(
    calls[0].init.body,
    JSON.stringify({ token: "a-token", password: "a-new-password-1234" }),
  );
});
