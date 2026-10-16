import assert from "node:assert/strict";
import { test } from "node:test";

import { extractClientIp, signClientIp } from "./clientIp.ts";

// Fixed vector, cross-checked against the Python side
// (backend/tests/security/test_client_ip.py::test_the_wire_format_matches_the_frontend_signer)
// so the two implementations of the SAME wire format cannot silently drift apart.
const SHARED_KEY = "shared-test-key-32-chars-long-00";
const EXPECTED_SIGNATURE =
  "dd60e23e6ade9717baed6319761684493fbc7f4e32d38d08349cf89dd958e43a".slice(0, 64);

test("the signed header matches the fixed cross-language vector", () => {
  const header = signClientIp(SHARED_KEY, "203.0.113.99", "POST", "/chat/stream", 1_700_000_000);
  assert.equal(header, `203.0.113.99|1700000000|${EXPECTED_SIGNATURE}`);
});

test("extractClientIp takes the right-most entry by default (no trusted hops)", () => {
  assert.equal(extractClientIp("203.0.113.1, 203.0.113.2", 0), "203.0.113.2");
});

test("extractClientIp skips N trusted hops from the right", () => {
  // e.g. Next's own server appended the socket address as the last entry: skip 1.
  assert.equal(extractClientIp("203.0.113.1, 203.0.113.2, 10.0.0.5", 1), "203.0.113.2");
});

test("extractClientIp never goes negative: too many trusted hops clamps to the first entry", () => {
  assert.equal(extractClientIp("203.0.113.1, 203.0.113.2", 5), "203.0.113.1");
});

test("extractClientIp returns null for an absent or empty header", () => {
  assert.equal(extractClientIp(null, 0), null);
  assert.equal(extractClientIp("", 0), null);
  assert.equal(extractClientIp("   ", 0), null);
});

test("signing the same inputs twice at the same second is deterministic", () => {
  const a = signClientIp(SHARED_KEY, "203.0.113.99", "GET", "/conversations", 42);
  const b = signClientIp(SHARED_KEY, "203.0.113.99", "GET", "/conversations", 42);
  assert.equal(a, b);
});

test("a different method, path or ip changes the signature", () => {
  const base = signClientIp(SHARED_KEY, "203.0.113.99", "GET", "/conversations", 42);
  assert.notEqual(signClientIp(SHARED_KEY, "203.0.113.99", "POST", "/conversations", 42), base);
  assert.notEqual(signClientIp(SHARED_KEY, "203.0.113.99", "GET", "/account", 42), base);
  assert.notEqual(signClientIp(SHARED_KEY, "203.0.113.100", "GET", "/conversations", 42), base);
});
