// T12.1.8 (D8): a UX-ONLY redirect, run at the edge before a protected page's shell is ever
// sent. Authorisation lives entirely in the backend: every /api/... call still goes through
// the BFF to it, which is the only place a session (or a forged/expired/revoked cookie) is
// actually verified -- this middleware only avoids flashing the app shell to a visitor who
// plainly has no session cookie at all. The real logic (lib/authGuard.ts) is Next.js-import
// free and tested directly with `node --test`; this file is an intentionally thin wrapper,
// like the BFF's own route handlers (C5 pattern).
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { shouldRedirectToLogin } from "@/lib/authGuard";

export function middleware(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;
  if (shouldRedirectToLogin(pathname, request.headers.get("cookie"))) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/chat/:path*", "/account/:path*"],
};
