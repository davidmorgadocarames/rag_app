// Nonce-based CSP (D12.2, T12.2.2/T12.2.3, E3): pure, Next.js-import-free so `node --test`
// can exercise it directly (C5 pattern) -- middleware.ts is the thin wrapper that supplies
// real randomness (the Edge runtime's Web Crypto API) and attaches the result to the
// response.
//
// CSP_MODE is the single switch E3 asks for. **Enforcing since 2026-10-17 (12a-E phase 2):**
// the Playwright page walk (`frontend/e2e/security-headers.spec.ts`, `scripts/gate.sh`'s
// `security-headers` step) ran clean in `"report-only"` first (zero violations) and then
// again after flipping this to `"enforce"` (still zero) -- T12.2.3's literal Done-when
// ("zero violations WITH CSP enforcing") is satisfied, not just the weaker report-only case.
export type CspMode = "report-only" | "enforce";

// A function, not a bare literal `const`: TypeScript narrows an unreassigned `const`
// initialised with a string literal down to that one literal type for every comparison in
// the program, even across an explicit wider annotation -- which would make the
// `CSP_HEADER_NAME` ternary below a compile error ("comparison has no overlap") if this ever
// moved back to a single fixed value. Wrapping it in a function whose declared return type
// is the union sidesteps that narrowing while still being exactly one line to change.
function cspMode(): CspMode {
  return "enforce";
}

export const CSP_MODE: CspMode = cspMode();

export const CSP_REPORT_PATH = "/api/csp-report";
export const REPORTING_GROUP = "csp-endpoint";

export const CSP_HEADER_NAME: string =
  CSP_MODE === "enforce" ? "Content-Security-Policy" : "Content-Security-Policy-Report-Only";

// `Reporting-Endpoints` (the current standard, read by Chromium's `report-to`) -- Firefox/
// Safari instead honour the legacy `report-uri` directive baked into the policy itself
// below, so both browser families reach the same backend route either way.
export const REPORTING_ENDPOINTS_HEADER = `${REPORTING_GROUP}="${CSP_REPORT_PATH}"`;

/** Base64-encode `randomBytes` (16 bytes of real randomness from the caller, T12.2.2). */
export function generateNonce(randomBytes: Uint8Array): string {
  let binary = "";
  for (const byte of randomBytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/**
 * The CSP directive string for one request's `nonce`.
 *
 * `script-src 'nonce-…' 'strict-dynamic'` (plan): only the nonce'd Next.js runtime scripts
 * (and whatever they load) may execute -- no inline/`unsafe-inline` fallback. `style-src
 * 'self' 'unsafe-inline'` (plan, E4): the app writes no inline `style={{}}`/`<style>` itself
 * (grepped by 12a-D's DA review), but Next's OWN CSS-in-JS/`next/font` machinery injects
 * `<style>` tags without a nonce, so `unsafe-inline` on styles ONLY is the plan's documented
 * trade-off. Everything else defaults to `'self'` (defense in depth, not contradicted by any
 * plan row). `report-uri`/`report-to` both point at the SAME backend route (E5) so every
 * browser, old or new, reports to it.
 */
export function buildCsp(nonce: string): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    `report-uri ${CSP_REPORT_PATH}`,
    `report-to ${REPORTING_GROUP}`,
  ].join("; ");
}
