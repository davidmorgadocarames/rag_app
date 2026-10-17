// Signed client IP (T12.1.6, T5/V7): the BFF derives the real client address from
// X-Forwarded-For and signs it with HMAC-SHA256 so the backend can trust it instead of the
// shared Azure-internal connection address it would otherwise see through the BFF.
//
// Wire format: "client_ip|timestamp|hex_hmac", hmac = HMAC-SHA256(key, "ip|method|path|ts").
// This MUST match the backend's rag_app.client_ip exactly -- backend/tests/security/
// test_client_ip.py and this file's own test are checked against the same fixed vectors so
// the two sides cannot silently drift apart.
//
// Plain TypeScript, no Next.js imports, so `node --test` can exercise it directly.

import { createHmac } from "node:crypto";

/**
 * The real client IP from a `X-Forwarded-For` header, skipping ``trustedHops`` entries from
 * the RIGHT. Azure's ingress appends the actual client address; depending on the runtime,
 * Next's own server MAY append the socket address after that (observed locally: measure and
 * set CLIENT_IP_TRUSTED_HOPS accordingly -- confirmed on Azure by the T12.6a.5 smoke, not
 * here). ``null`` when the header is absent or empty.
 */
export function extractClientIp(xff: string | null, trustedHops: number): string | null {
  if (!xff) return null;
  const parts = xff
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  if (parts.length === 0) return null;
  const index = parts.length - 1 - Math.max(0, trustedHops);
  return parts[index >= 0 ? index : 0];
}

function signature(key: string, clientIp: string, method: string, path: string, timestamp: number): string {
  const message = `${clientIp}|${method}|${path}|${timestamp}`;
  return createHmac("sha256", key).update(message).digest("hex");
}

/** Build the `X-SecRAG-Client-IP` header value. ``now`` is in whole seconds (Unix time). */
export function signClientIp(
  key: string,
  clientIp: string,
  method: string,
  path: string,
  now: number = Math.floor(Date.now() / 1000),
): string {
  return `${clientIp}|${now}|${signature(key, clientIp, method, path, now)}`;
}
