import assert from "node:assert/strict";
import { test } from "node:test";

import { SECURITY_HEADERS } from "./securityHeaders.mjs";

function value(key: string): string | undefined {
  return SECURITY_HEADERS.find((h) => h.key === key)?.value;
}

test("HSTS is set with includeSubDomains but never preload (E7)", () => {
  const hsts = value("Strict-Transport-Security");
  assert.ok(hsts);
  assert.match(hsts!, /includeSubDomains/);
  assert.doesNotMatch(hsts!, /preload/);
});

test("nosniff, Referrer-Policy and Permissions-Policy are all present (T12.2.1)", () => {
  assert.equal(value("X-Content-Type-Options"), "nosniff");
  assert.equal(value("Referrer-Policy"), "strict-origin-when-cross-origin");
  assert.ok(value("Permissions-Policy"));
});
