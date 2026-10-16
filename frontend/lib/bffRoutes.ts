// The BFF's explicit route allow-list (P12-D1, T12.1.5): today's backend routes only. Never
// /metrics, /docs, /redoc, /openapi.json -- anything not listed here is a 404, by
// construction (there is no catch-all "forward everything" branch).
//
// Plain TypeScript, no Next.js imports, so `node --test` can exercise it directly.

export interface AllowedRoute {
  method: string;
  pattern: RegExp;
}

export const ALLOWED_ROUTES: readonly AllowedRoute[] = [
  { method: "POST", pattern: /^\/auth\/register$/ },
  { method: "POST", pattern: /^\/auth\/login$/ },
  { method: "POST", pattern: /^\/auth\/logout$/ },
  { method: "GET", pattern: /^\/auth\/me$/ },
  { method: "GET", pattern: /^\/auth\/verify$/ },
  { method: "POST", pattern: /^\/auth\/resend-verification$/ },
  { method: "DELETE", pattern: /^\/account$/ },
  { method: "POST", pattern: /^\/chat\/stream$/ },
  { method: "GET", pattern: /^\/conversations$/ },
  { method: "GET", pattern: /^\/conversations\/[^/]+$/ },
  { method: "PATCH", pattern: /^\/conversations\/[^/]+$/ },
  { method: "DELETE", pattern: /^\/conversations\/[^/]+$/ },
];

/** Whether ``method path`` (upstream path, no "/api" prefix) is on the allow-list. */
export function isAllowedUpstreamPath(method: string, path: string): boolean {
  return ALLOWED_ROUTES.some((route) => route.method === method && route.pattern.test(path));
}
