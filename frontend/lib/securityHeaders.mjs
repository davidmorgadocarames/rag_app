// Static response headers (T12.2.1): applied to EVERY route including `_next/static` assets
// via `next.config.mjs`'s `headers()` -- unlike the per-request CSP (middleware.ts/lib/csp.ts),
// these never need a nonce and are identical on every response. Plain `.mjs` (not `.ts`): it
// is imported directly by `next.config.mjs`, which Next.js loads with Node's own ESM loader
// BEFORE any TypeScript/SWC pipeline exists -- a `.ts` import there is not reliable.
//
// HSTS deliberately has NO `preload` (E7): the deployed hostname is a SHARED
// `*.azurecontainerapps.io` domain the project does not own outright -- submitting it to the
// browser preload list would be effectively permanent and would also assert HTTPS for every
// OTHER app sharing a sibling subdomain under that same suffix, which this project cannot
// promise. `includeSubDomains` without `preload` still forces HTTPS for this app's own
// subdomains once a browser has seen the header once.
export const SECURITY_HEADERS = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];
