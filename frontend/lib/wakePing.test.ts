import assert from "node:assert/strict";
import { test } from "node:test";

import { sendWakePing } from "./wakePing.ts";

test("sendWakePing: GETs /api/wake exactly once, fire-and-forget", () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  const fakeFetch = (url: string, init?: RequestInit) => {
    calls.push([url, init]);
    return Promise.resolve(new Response(null, { status: 204 }));
  };

  sendWakePing(fakeFetch as unknown as typeof fetch);

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/wake");
  assert.equal(calls[0][1]?.method, "GET");
});

test("sendWakePing: a rejected fetch promise never throws or surfaces (best-effort)", () => {
  const fakeFetch = () => Promise.reject(new Error("network down"));
  assert.doesNotThrow(() => sendWakePing(fakeFetch as unknown as typeof fetch));
});

test("sendWakePing: a fetch implementation that throws synchronously never escapes either", () => {
  const fakeFetch = () => {
    throw new Error("boom");
  };
  assert.doesNotThrow(() => sendWakePing(fakeFetch as unknown as typeof fetch));
});

test("sendWakePing: never sends a body or credentials beyond the plain GET", () => {
  const calls: Array<RequestInit | undefined> = [];
  const fakeFetch = (_url: string, init?: RequestInit) => {
    calls.push(init);
    return Promise.resolve(new Response(null, { status: 204 }));
  };

  sendWakePing(fakeFetch as unknown as typeof fetch);

  assert.equal(calls[0]?.body, undefined);
});
