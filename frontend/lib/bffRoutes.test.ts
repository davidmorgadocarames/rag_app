import assert from "node:assert/strict";
import { test } from "node:test";

import { isAllowedUpstreamPath } from "./bffRoutes.ts";

test("the allow-listed backend routes pass", () => {
  assert.equal(isAllowedUpstreamPath("POST", "/auth/register"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/auth/login"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/auth/logout"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/auth/me"), true);
  // T12.4.5: POST now (body token), not a GET query param.
  assert.equal(isAllowedUpstreamPath("POST", "/auth/verify"), true);
  // T12.4.4: resend is reached by email now (unauthenticated), still POST.
  assert.equal(isAllowedUpstreamPath("POST", "/auth/resend-verification"), true);
  assert.equal(isAllowedUpstreamPath("DELETE", "/account"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/chat/stream"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/conversations"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/conversations/abc-123"), true);
  assert.equal(isAllowedUpstreamPath("PATCH", "/conversations/abc-123"), true);
  assert.equal(isAllowedUpstreamPath("DELETE", "/conversations/abc-123"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/csp-report"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/account/mfa/enroll"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/account/mfa/confirm"), true);
  assert.equal(isAllowedUpstreamPath("POST", "/account/mfa/step-up"), true);
  assert.equal(isAllowedUpstreamPath("GET", "/admin/ping"), true);
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
  // T12.4.5: the old GET-consumes-the-token shape is gone -- a mail scanner pre-fetching
  // the frontend's /verify?token=... page can no longer reach a GET that would consume it.
  assert.equal(isAllowedUpstreamPath("GET", "/auth/verify"), false);
});

test("T12.4.6 (12b-C) has not landed yet: /auth/reset is not allow-listed", () => {
  assert.equal(isAllowedUpstreamPath("POST", "/auth/reset"), false);
});

test("the bare /chat route (legacy, non-stream) is not on the BFF allow-list", () => {
  assert.equal(isAllowedUpstreamPath("POST", "/chat"), false);
});

test("an unknown path is a 404 by construction, not a special case", () => {
  assert.equal(isAllowedUpstreamPath("GET", "/anything/else"), false);
});
