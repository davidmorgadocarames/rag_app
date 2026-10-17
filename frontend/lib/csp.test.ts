import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CSP_HEADER_NAME,
  CSP_MODE,
  CSP_REPORT_PATH,
  REPORTING_ENDPOINTS_HEADER,
  buildCsp,
  generateNonce,
} from "./csp.ts";

test("generateNonce base64-encodes the given bytes deterministically", () => {
  const bytes = new Uint8Array([0, 1, 2, 253, 254, 255]);
  const nonce = generateNonce(bytes);
  assert.equal(nonce, Buffer.from(bytes).toString("base64"));
});

test("generateNonce differs for different random bytes", () => {
  const a = generateNonce(new Uint8Array(16).fill(1));
  const b = generateNonce(new Uint8Array(16).fill(2));
  assert.notEqual(a, b);
});

test("buildCsp embeds the nonce in script-src with strict-dynamic, never unsafe-inline for scripts", () => {
  const csp = buildCsp("abc123==");
  assert.match(csp, /script-src 'self' 'nonce-abc123==' 'strict-dynamic'/);
  assert.doesNotMatch(csp.split(";").find((d) => d.includes("script-src")) ?? "", /unsafe-inline/);
});

test("buildCsp allows unsafe-inline only on style-src (E4, Next's own CSS-in-JS)", () => {
  const csp = buildCsp("n");
  assert.match(csp, /style-src 'self' 'unsafe-inline'/);
});

test("buildCsp sets frame-ancestors 'none' and both reporting directives", () => {
  const csp = buildCsp("n");
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, new RegExp(`report-uri ${CSP_REPORT_PATH}`));
  assert.match(csp, /report-to csp-endpoint/);
});

test("CSP_MODE is enforce, proven by the Playwright walk (T12.2.3)", () => {
  // Flipping this back to "report-only" without a reason (a real regression found by the
  // `security-headers` gate step) would silently weaken the deployed policy -- see csp.ts's
  // module comment for when and why this changed.
  assert.equal(CSP_MODE, "enforce");
  assert.equal(CSP_HEADER_NAME, "Content-Security-Policy");
});

test("REPORTING_ENDPOINTS_HEADER names the same path the policy's report-uri/report-to use", () => {
  assert.equal(REPORTING_ENDPOINTS_HEADER, `csp-endpoint="${CSP_REPORT_PATH}"`);
});
