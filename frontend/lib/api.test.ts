import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ApiError,
  CSRF_COOKIE_NAME,
  deleteAccount,
  login,
  logout,
  me,
  readCookie,
  register,
} from "./api.ts";

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
