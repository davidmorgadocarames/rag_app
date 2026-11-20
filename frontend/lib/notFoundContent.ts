// Pure copy/content model for app/not-found.tsx (T13.10.1, DEBT-057). Deliberately has NO
// field for the requested path/URL at all -- by construction there is nothing here a page
// could echo back, which is the actual guarantee behind T13.10.3's "no path echo" test (the
// 404 page component never calls `usePathname()`/`useSearchParams()` either, so it never
// even SEES the path it was asked to render for). And deliberately only one destination:
// `BACK_TO_LOGIN_HREF` is the only link/button target this page exposes (user: extra links on
// an error page are attack surface / an information leak about the app's structure).
export const NOT_FOUND_CODE = "404";
export const NOT_FOUND_LABEL = "room not found";
export const NOT_FOUND_HEADING = "This room isn't in the vault.";
export const NOT_FOUND_BODY =
  "The address you followed doesn't match any page in SecRAG. It may be mistyped, or the page may have moved.";
export const BACK_TO_LOGIN_LABEL = "Back to login";
export const BACK_TO_LOGIN_HREF = "/login";
