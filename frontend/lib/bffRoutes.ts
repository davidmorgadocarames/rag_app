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
  // T12.4.5: POST now (body token), not a GET query param -- a mail scanner pre-fetching
  // the /verify?token=... page's own GET can never consume the token any more.
  { method: "POST", pattern: /^\/auth\/verify$/ },
  { method: "POST", pattern: /^\/auth\/resend-verification$/ },
  // T12.4.6 (12b-C): forgot-password request + the reset page's token+password submit.
  { method: "POST", pattern: /^\/auth\/forgot-password$/ },
  { method: "POST", pattern: /^\/auth\/reset$/ },
  { method: "DELETE", pattern: /^\/account$/ },
  { method: "POST", pattern: /^\/chat\/stream$/ },
  { method: "GET", pattern: /^\/conversations$/ },
  { method: "GET", pattern: /^\/conversations\/[^/]+$/ },
  { method: "PATCH", pattern: /^\/conversations\/[^/]+$/ },
  { method: "DELETE", pattern: /^\/conversations\/[^/]+$/ },
  // T12.2.2/E5: the CSP violation reporter -- unauthenticated, no session, explicitly
  // CSRF-exempt on the backend (rag_app.csp_report); still only ever POST.
  { method: "POST", pattern: /^\/csp-report$/ },
  // Admin MFA (T12.3.4/T12.3.5, code written in 12b-A -- frontend verification is Phase 2):
  // the backend itself still gates every one of these with rag_app.authz (permission map +
  // MFA step-up) -- the allow-list only decides what reaches the backend at all.
  { method: "POST", pattern: /^\/account\/mfa\/enroll$/ },
  { method: "POST", pattern: /^\/account\/mfa\/confirm$/ },
  { method: "POST", pattern: /^\/account\/mfa\/step-up$/ },
  { method: "GET", pattern: /^\/admin\/ping$/ },
];

/** Whether ``method path`` (upstream path, no "/api" prefix) is on the allow-list. */
export function isAllowedUpstreamPath(method: string, path: string): boolean {
  return ALLOWED_ROUTES.some((route) => route.method === method && route.pattern.test(path));
}
