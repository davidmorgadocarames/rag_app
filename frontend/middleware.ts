// T12.1.8 (D8) + T12.2.2/T12.2.3 (E3): two independent jobs on every HTML route.
//
// 1. A UX-ONLY redirect for the protected paths, run at the edge before a protected page's
//    shell is ever sent. Authorisation lives entirely in the backend: every /api/... call
//    still goes through the BFF to it, which is the only place a session (or a forged/
//    expired/revoked cookie) is actually verified -- this middleware only avoids flashing
//    the app shell to a visitor who plainly has no session cookie at all. The real logic
//    (lib/authGuard.ts) is Next.js-import-free and tested directly with `node --test`.
// 2. A fresh per-request CSP nonce (lib/csp.ts, also Next.js-import-free/tested): set on
//    BOTH the outgoing request headers (so the App Router can apply it to the scripts it
//    injects itself -- Next's documented nonce mechanism, see
//    https://nextjs.org/docs/app/guides/content-security-policy) and the response. Every
//    page under the matcher below renders dynamically (D12.2): a nonce baked into a
//    statically-generated page would be reused by every visitor, defeating the point.
//
// This file is an intentionally thin wrapper around both pure modules, like the BFF's own
// route handlers (C5 pattern) -- never unit-tested directly, only wired.
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { shouldRedirectToLogin } from "@/lib/authGuard";
import { CSP_HEADER_NAME, REPORTING_ENDPOINTS_HEADER, buildCsp, generateNonce } from "@/lib/csp";

export function middleware(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;

  const nonceBytes = new Uint8Array(16);
  crypto.getRandomValues(nonceBytes);
  const nonce = generateNonce(nonceBytes);
  const csp = buildCsp(nonce);

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = shouldRedirectToLogin(pathname, request.headers.get("cookie"))
    ? NextResponse.redirect(new URL("/login", request.url))
    : NextResponse.next({ request: { headers: requestHeaders } });

  response.headers.set(CSP_HEADER_NAME, csp);
  response.headers.set("Reporting-Endpoints", REPORTING_ENDPOINTS_HEADER);
  return response;
}

export const config = {
  // Every HTML route EXCEPT: Next's own static/image assets (no nonce needed, and the
  // matcher must not force every asset byte through this function), the favicon, and
  // /api/... (the BFF proxy -- a nonce/CSP header on a JSON response is meaningless and
  // T12.2.2's `report-uri`/`report-to` target, /api/csp-report, must stay reachable
  // unconditionally rather than depend on this same middleware running first).
  matcher: ["/((?!_next/static|_next/image|favicon.ico|api/).*)"],
};
