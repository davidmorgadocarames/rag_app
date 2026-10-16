// Server-only configuration for the BFF route handlers (P12-D1). BACKEND_URL is deliberately
// NOT a NEXT_PUBLIC_ variable: it must never be baked into the client bundle (the browser
// talks only to this server, which talks to the backend).

import type { ProxyConfig } from "./bffProxy.ts";

// DEC-006: the Azure ingress's own ~240s cutoff must stay the only limit a slow upstream
// (a cold backend, a slow answer) hits -- the BFF's own timeout must never be shorter.
export const MIN_TIMEOUT_MS = 240_000;

export function bffConfig(env: Record<string, string | undefined> = process.env): ProxyConfig {
  const backendUrl = env.BACKEND_URL;
  if (!backendUrl) {
    throw new Error("BACKEND_URL is not set (server-side only -- never NEXT_PUBLIC_BACKEND_URL)");
  }
  const requested = Number(env.BFF_TIMEOUT_MS);
  const timeoutMs = Number.isFinite(requested) && requested > 0
    ? Math.max(MIN_TIMEOUT_MS, requested)
    : MIN_TIMEOUT_MS; // fmt: skip
  const clientIpKeys = (env.CLIENT_IP_HMAC_KEYS ?? "")
    .split(",")
    .map((key) => key.trim())
    .filter((key) => key.length > 0);
  const trustedHops = Number(env.CLIENT_IP_TRUSTED_HOPS ?? "0");
  return {
    backendUrl,
    timeoutMs,
    clientIpKeys,
    trustedHops: Number.isFinite(trustedHops) ? trustedHops : 0,
  };
}
