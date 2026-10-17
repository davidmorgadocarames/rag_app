import assert from "node:assert/strict";
import { test } from "node:test";

import { isAllowedUpstreamPath } from "./bffRoutes.ts";

test("the allow-listed backend routes pass", () => {
  assert.equal(isAllowedUpstreamPath("POST", "/auth/register"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/auth/login"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/auth/logout"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/auth/me"), true);
  assert.equal(isAllowedUpstreamPath("DELETE", "/account"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/chat/stream"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/conversations"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/conversations/abc-123"), true);
  assert.equal(isAllowedUpstreamPath("PATCH", "/conversations/abc-123"), true);
  assert.equal(isAllowedUpstreamPath("DELETE", "/conversations/abc-123"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/csp-report"), true);
});

test("never /metrics, /docs, /redoc or /openapi.json, whatever the method", () => {
  for (const path of ["/metrics", "/docs", "/redoc", "/openapi.json"]) {
    for (const method of ["GET", "POST"]) {
      assert.equal(isAllowedUpstreamPath(method, path), false, `${method} ${path}`);
    }
  }
});

test("a listed path with the wrong method is refused", () => {
  assert.equal(isAllowedUpstreamPath("DELETE", "/auth/me"), false);
  assert.equal(isAllowedUpstreamPath("POST", "/conversations"), false);
});

test("the bare /chat route (legacy, non-stream) is not on the BFF allow-list", () => {
  assert.equal(isAllowedUpstreamPath("POST", "/chat"), false);
});

test("an unknown path is a 404 by construction, not a special case", () => {
  assert.equal(isAllowedUpstreamPath("GET", "/anything/else"), false);
});
