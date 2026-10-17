// Pure logic behind middleware.ts's UX-only redirect (T12.1.8, D8): the session cookie's
// mere PRESENCE decides whether to show the login page or the app shell before the page
// even renders. This is explicitly NOT an authorization check -- a present-but-invalid,
// expired or forged cookie still passes this guard; the backend is the only place that
// verifies a session (every /api/... call goes through the BFF to it), and the pages
// themselves re-check by calling the API and redirecting again on a 401 (T12.1.9's "CSR"
// half). Kept Next.js-import-free so `node --test` can exercise it directly (C5 pattern).

export const SESSION_COOKIE_NAME = "__Host-secrag_session";

// Routes a visitor should never see the shell of without (at least) a session cookie.
export const PROTECTED_PATHS = ["/chat", "/account"] as const;

export function isProtectedPath(pathname: string): boolean {
  return PROTECTED_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function hasSessionCookie(cookieHeader: string | null | undefined): boolean {
  if (!cookieHeader) return false;
  return cookieHeader.split(";").some((part) => {
    const i = part.indexOf("=");
    const name = i < 0 ? part : part.slice(0, i);
    return name.trim() === SESSION_COOKIE_NAME;
  });
}

export function shouldRedirectToLogin(
  pathname: string,
  cookieHeader: string | null | undefined,
): boolean {
  return isProtectedPath(pathname) && !hasSessionCookie(cookieHeader);
}
