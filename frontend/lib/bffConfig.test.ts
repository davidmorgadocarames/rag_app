import assert from "node:assert/strict";
import { test } from "node:test";

import { MIN_TIMEOUT_MS, bffConfig } from "./bffConfig.ts";

test("BACKEND_URL is required", () => {
  assert.throws(() => bffConfig({}), /BACKEND_URL/);
});

test("the default timeout is at least the Azure ingress cutoff (DEC-006)", () => {
  const config = bffConfig({ BACKEND_URL: "http://backend:8000" });
  assert.equal(config.timeoutMs, MIN_TIMEOUT_MS);
  assert.ok(config.timeoutMs >= 240_000);
});

test("a configured BFF_TIMEOUT_MS below the floor is clamped up, never down", () => {
  const config = bffConfig({ BACKEND_URL: "http://backend:8000", BFF_TIMEOUT_MS: "1000" });
  assert.equal(config.timeoutMs, MIN_TIMEOUT_MS);
});

test("a configured BFF_TIMEOUT_MS above the floor is honoured", () => {
  const config = bffConfig({ BACKEND_URL: "http://backend:8000", BFF_TIMEOUT_MS: "300000" });
  assert.equal(config.timeoutMs, 300_000);
});

test("CLIENT_IP_HMAC_KEYS is parsed, trimmed and empty entries dropped", () => {
  const config = bffConfig({
    BACKEND_URL: "http://backend:8000",
    CLIENT_IP_HMAC_KEYS: " current-key , previous-key ,,",
  });
  assert.deepEqual(config.clientIpKeys, ["current-key", "previous-key"]);
});

test("no CLIENT_IP_HMAC_KEYS means signing is disabled", () => {
  const config = bffConfig({ BACKEND_URL: "http://backend:8000" });
  assert.deepEqual(config.clientIpKeys, []);
});

test("CLIENT_IP_TRUSTED_HOPS defaults to 0 and parses a configured value", () => {
  assert.equal(bffConfig({ BACKEND_URL: "http://backend:8000" }).trustedHops, 0);
  assert.equal(
    bffConfig({ BACKEND_URL: "http://backend:8000", CLIENT_IP_TRUSTED_HOPS: "1" }).trustedHops,
    1,
  );
});
