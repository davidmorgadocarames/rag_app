// The BFF's core proxy logic (P12-D1, T12.1.5): an explicit allow-list (bffRoutes.ts),
// cookie forwarding both ways, unbuffered SSE, a timeout well above Azure's ~240s ingress
// cutoff (DEC-006), access_token stripped from login/register bodies (V15), and a signed
// client IP header (T5/V7, T12.1.6).
//
// Kept as a plain, testable module (C5): the actual Next.js route handlers
// (app/api/[...path]/route.ts, app/api/health/route.ts) are thin wrappers around this file
// and bffConfig.ts, so `node --test` can exercise the real logic against a fake upstream
// without the Next.js runtime.

import { isAllowedUpstreamPath } from "./bffRoutes.ts";
import { extractClientIp, signClientIp } from "./clientIp.ts";

export interface ProxyRequest {
  method: string;
  path: string; // upstream path, e.g. "/auth/login" -- no "/api" prefix
  search: string; // "" or "?a=b"
  headers: Headers;
  body: ReadableStream<Uint8Array> | null;
}

export interface ProxyConfig {
  backendUrl: string;
  timeoutMs: number;
  // Current key first; previous key(s) kept verifiable during a rotation. Empty = signing
  // disabled (today's normal path until the feature is adopted, matching the backend).
  clientIpKeys: readonly string[];
  trustedHops: number;
  fetchImpl?: typeof fetch;
  now?: () => number; // seconds; overridable for deterministic tests
}

const STRIP_ACCESS_TOKEN_PATHS = new Set(["/auth/register", "/auth/login"]);
// Only these are ever forwarded upstream -- never a client-supplied Host/Connection/etc.
const FORWARDED_REQUEST_HEADERS = ["cookie", "x-csrf-token", "origin", "content-type", "accept"];
const CLIENT_IP_HEADER = "x-secrag-client-ip";

function jsonResponse(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function buildUpstreamHeaders(req: ProxyRequest, config: ProxyConfig): Headers {
  // A fresh Headers object with only the names in FORWARDED_REQUEST_HEADERS copied over:
  // an incoming X-SecRAG-Client-IP (or anything else) from the client is never forwarded --
  // the ONLY way this header reaches the backend is the signing block below, by the BFF
  // itself, from X-Forwarded-For.
  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  const currentKey = config.clientIpKeys.find((key) => key);
  if (currentKey) {
    const ip = extractClientIp(req.headers.get("x-forwarded-for"), config.trustedHops);
    if (ip) {
      const now = config.now ? config.now() : undefined;
      headers.set(CLIENT_IP_HEADER, signClientIp(currentKey, ip, req.method, req.path, now));
    }
  }
  return headers;
}

function getSetCookies(headers: Headers): string[] {
  const withGetSetCookie = headers as Headers & { getSetCookie?: () => string[] };
  if (typeof withGetSetCookie.getSetCookie === "function") return withGetSetCookie.getSetCookie();
  const single = headers.get("set-cookie");
  return single ? [single] : [];
}

function forwardResponseHeaders(upstream: Response): Headers {
  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (lower === "set-cookie" || lower === "content-length") return;
    out.set(key, value);
  });
  for (const cookie of getSetCookies(upstream.headers)) out.append("set-cookie", cookie);
  return out;
}

async function stripAccessToken(upstream: Response, headers: Headers): Promise<Response> {
  const text = await upstream.text();
  let body = text;
  try {
    const data = JSON.parse(text) as Record<string, unknown>;
    if (data && typeof data === "object" && "access_token" in data) {
      delete data.access_token;
      body = JSON.stringify(data);
    }
  } catch {
    // not JSON: forward unchanged
  }
  return new Response(body, { status: upstream.status, headers });
}

/** Proxy one request to the backend, or 404 when its (method, path) is not allow-listed. */
export async function proxyToBackend(req: ProxyRequest, config: ProxyConfig): Promise<Response> {
  if (!isAllowedUpstreamPath(req.method, req.path)) return jsonResponse(404, "not found");

  const fetchImpl = config.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  let upstream: Response;
  try {
    upstream = await fetchImpl(`${config.backendUrl}${req.path}${req.search}`, {
      method: req.method,
      headers: buildUpstreamHeaders(req, config),
      body: req.body,
      // Node's fetch (undici) requires `duplex` to send a streamed request body.
      duplex: req.body ? "half" : undefined,
      signal: controller.signal,
    } as RequestInit & { duplex?: "half" });
  } catch (err) {
    clearTimeout(timer);
    const aborted = err instanceof Error && err.name === "AbortError";
    return jsonResponse(502, aborted ? "upstream timed out" : "upstream unreachable");
  }
  // The timeout only guards "the upstream never even answered" (DEC-006): once headers
  // arrive, the BFF must pass the (possibly slow, token-by-token) body through unbuffered
  // with no further cutoff of its own -- the Azure ingress stays the only limit.
  clearTimeout(timer);

  const headers = forwardResponseHeaders(upstream);
  if (STRIP_ACCESS_TOKEN_PATHS.has(req.path)) return stripAccessToken(upstream, headers);
  return new Response(upstream.body, { status: upstream.status, headers });
}
