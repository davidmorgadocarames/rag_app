import assert from "node:assert/strict";
import { test } from "node:test";

import { proxyToBackend } from "./bffProxy.ts";
import type { ProxyConfig, ProxyRequest } from "./bffProxy.ts";

function baseConfig(overrides: Partial<ProxyConfig> = {}): ProxyConfig {
  return {
    backendUrl: "http://backend:8000",
    timeoutMs: 240_000,
    clientIpKeys: [],
    trustedHops: 0,
    ...overrides,
  };
}

function baseRequest(overrides: Partial<ProxyRequest> = {}): ProxyRequest {
  return {
    method: "GET",
    path: "/conversations",
    search: "",
    headers: new Headers(),
    body: null,
    ...overrides,
  };
}

test("a non-allow-listed path is a 404 before any fetch happens", async () => {
  let called = false;
  const fetchImpl = (async () => {
    called = true;
    return new Response("should never be reached");
  }) as typeof fetch;

  const response = await proxyToBackend(
    baseRequest({ method: "GET", path: "/metrics" }),
    baseConfig({ fetchImpl }),
  );
  assert.equal(response.status, 404);
  assert.equal(called, false);
});

test("Set-Cookie passes through, the incoming Cookie header is forwarded upstream", async () => {
  let sentHeaders: Headers | undefined;
  const fetchImpl = (async (_url, init) => {
    sentHeaders = new Headers(init?.headers as HeadersInit);
    const upstreamHeaders = new Headers();
    upstreamHeaders.append("set-cookie", "a=1; Path=/");
    upstreamHeaders.append("set-cookie", "b=2; Path=/");
    return new Response(JSON.stringify({ detail: "ok" }), { status: 200, headers: upstreamHeaders });
  }) as typeof fetch;

  const response = await proxyToBackend(
    baseRequest({
      method: "POST",
      path: "/auth/logout",
      headers: new Headers({ cookie: "__Host-secrag_session=raw" }),
    }),
    baseConfig({ fetchImpl }),
  );

  assert.equal(sentHeaders?.get("cookie"), "__Host-secrag_session=raw");
  const setCookies = response.headers.getSetCookie
    ? response.headers.getSetCookie()
    : [response.headers.get("set-cookie") ?? ""];
  assert.deepEqual(setCookies.sort(), ["a=1; Path=/", "b=2; Path=/"]);
});

test("Origin and X-CSRF-Token are forwarded to the upstream call", async () => {
  let sentHeaders: Headers | undefined;
  const fetchImpl = (async (_url, init) => {
    sentHeaders = new Headers(init?.headers as HeadersInit);
    return new Response(JSON.stringify({ detail: "ok" }), { status: 200 });
  }) as typeof fetch;

  await proxyToBackend(
    baseRequest({
      method: "POST",
      path: "/auth/logout",
      headers: new Headers({
        origin: "http://localhost:3000",
        "x-csrf-token": "abc123",
      }),
    }),
    baseConfig({ fetchImpl }),
  );

  assert.equal(sentHeaders?.get("origin"), "http://localhost:3000");
  assert.equal(sentHeaders?.get("x-csrf-token"), "abc123");
});

test("access_token is stripped from the login response body (V15)", async () => {
  const fetchImpl = (async () =>
    new Response(JSON.stringify({ access_token: "jwt-secret", other: "kept" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })) as typeof fetch;

  const response = await proxyToBackend(
    baseRequest({ method: "POST", path: "/auth/login" }),
    baseConfig({ fetchImpl }),
  );
  const body = (await response.json()) as Record<string, unknown>;
  assert.equal("access_token" in body, false);
  assert.equal(body.other, "kept");
});

test("access_token is stripped from the register response body too", async () => {
  const fetchImpl = (async () =>
    new Response(JSON.stringify({ access_token: "jwt-secret" }), { status: 201 })) as typeof fetch;

  const response = await proxyToBackend(
    baseRequest({ method: "POST", path: "/auth/register" }),
    baseConfig({ fetchImpl }),
  );
  const body = (await response.json()) as Record<string, unknown>;
  assert.equal("access_token" in body, false);
});

test("a non-auth response body is passed through byte-for-byte, untouched", async () => {
  const fetchImpl = (async () =>
    new Response(JSON.stringify({ access_token: "not-really-login-related" }), {
      status: 200,
    })) as typeof fetch;

  const response = await proxyToBackend(
    baseRequest({ method: "GET", path: "/conversations" }),
    baseConfig({ fetchImpl }),
  );
  const body = (await response.json()) as Record<string, unknown>;
  // /conversations is not a stripping path: an access_token-shaped field (unlikely in
  // practice, but this proves the allow-list -- not a generic key name -- decides stripping.
  assert.equal(body.access_token, "not-really-login-related");
});

test("SSE chunks pass through progressively, unbuffered, in order", async () => {
  const encoder = new TextEncoder();
  const chunks = ['data: {"type":"conversation"}\n\n', 'data: {"type":"token"}\n\n', "data: {\"type\":\"done\"}\n\n"];
  const fetchImpl = (async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    return new Response(body, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as typeof fetch;

  const response = await proxyToBackend(
    baseRequest({ method: "POST", path: "/chat/stream" }),
    baseConfig({ fetchImpl }),
  );
  assert.ok(response.body);
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  const received: string[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received.push(decoder.decode(value));
  }
  assert.deepEqual(received, chunks);
});

test("a signed X-SecRAG-Client-IP is attached once CLIENT_IP_HMAC_KEYS is configured", async () => {
  let sentHeaders: Headers | undefined;
  const fetchImpl = (async (_url, init) => {
    sentHeaders = new Headers(init?.headers as HeadersInit);
    return new Response(JSON.stringify({ detail: "ok" }));
  }) as typeof fetch;

  await proxyToBackend(
    baseRequest({
      method: "GET",
      path: "/conversations",
      headers: new Headers({ "x-forwarded-for": "203.0.113.5" }),
    }),
    baseConfig({ fetchImpl, clientIpKeys: ["a-shared-secret-key"], now: () => 1_700_000_000 }),
  );
  const header = sentHeaders?.get("x-secrag-client-ip");
  assert.ok(header?.startsWith("203.0.113.5|1700000000|"));
});

test("no X-SecRAG-Client-IP is sent when signing is disabled (no keys configured)", async () => {
  let sentHeaders: Headers | undefined;
  const fetchImpl = (async (_url, init) => {
    sentHeaders = new Headers(init?.headers as HeadersInit);
    return new Response(JSON.stringify({ detail: "ok" }));
  }) as typeof fetch;

  await proxyToBackend(
    baseRequest({
      method: "GET",
      path: "/conversations",
      headers: new Headers({ "x-forwarded-for": "203.0.113.5" }),
    }),
    baseConfig({ fetchImpl }),
  );
  assert.equal(sentHeaders?.has("x-secrag-client-ip"), false);
});

test("DEC-006: the BFF's own timeout never fires before the configured duration", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });

  let capturedSignal: AbortSignal | undefined;
  let resolveFetch: (value: Response) => void = () => {};
  const fetchImpl = ((_url: string, init?: RequestInit) => {
    capturedSignal = init?.signal ?? undefined;
    return new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
  }) as typeof fetch;

  const resultPromise = proxyToBackend(
    baseRequest({ method: "POST", path: "/chat/stream" }),
    baseConfig({ fetchImpl, timeoutMs: 240_000 }),
  );

  t.mock.timers.tick(239_999);
  assert.equal(capturedSignal?.aborted, false);
  t.mock.timers.tick(1); // now exactly at the configured timeout
  assert.equal(capturedSignal?.aborted, true);

  resolveFetch(new Response("late but still here", { status: 200 }));
  return resultPromise.then((response) => {
    assert.equal(response.status, 200);
  });
});
