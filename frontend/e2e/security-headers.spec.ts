// T12.2.3/T12.2.5 (E3/E9): the Playwright page walk against the REAL built app in a real
// browser that proves (a) every static header (T12.2.1) and the per-request nonce CSP
// (T12.2.2) are present on every page's top-level response, logged in and out, and (b) zero
// `script-src` (or any other directive) violation is ever triggered -- Chromium logs a
// console message for a violation under BOTH report-only and enforced CSP alike, so this
// walk is meaningful before AND after `CSP_MODE` (frontend/lib/csp.ts) flips to `"enforce"`;
// T12.2.3's own Done-when ("zero violations WITH CSP enforcing") is only actually proven
// once `CSP_MODE` is `"enforce"` and this spec is green.
//
// `scripts/gate.sh`'s `security-headers` step starts the same native backend+frontend pair
// as `e2e`/`zap-baseline` (`e2e_start_backend`/`e2e_start_frontend`); --full only, never CI
// (DEBT-029 keeps the 5 required job names pinned).
import { expect, test } from "@playwright/test";
import type { ConsoleMessage, Page, Response } from "@playwright/test";

import { openVaultDoor, registerAndReachChat, uniqueEmail } from "./helpers.ts";

const CSP_VIOLATION_RE = /content security policy|refused to/i;

function assertStaticHeaders(response: Response | null, path: string): void {
  expect(response, `no response for ${path}`).not.toBeNull();
  const headers = response!.headers();
  expect(headers["strict-transport-security"], path).toContain("includeSubDomains");
  expect(headers["strict-transport-security"], path).not.toContain("preload");
  expect(headers["x-content-type-options"], path).toBe("nosniff");
  expect(headers["referrer-policy"], path).toBe("strict-origin-when-cross-origin");
  expect(headers["permissions-policy"], path).toBeTruthy();
}

function assertCsp(response: Response | null, path: string): void {
  const headers = response!.headers();
  // Whichever header name is active (lib/csp.ts's CSP_MODE single switch) must be present
  // with the real directives -- never silently absent on some pages only.
  const csp = headers["content-security-policy"] ?? headers["content-security-policy-report-only"];
  expect(csp, `no CSP header on ${path}`).toBeTruthy();
  expect(csp, path).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
  expect(csp, path).toMatch(/style-src 'self' 'unsafe-inline'/);
  expect(csp, path).toMatch(/frame-ancestors 'none'/);
  expect(csp, path).toMatch(/report-uri \/api\/csp-report/);
  expect(headers["reporting-endpoints"], path).toContain("/api/csp-report");
}

async function visit(page: Page, path: string): Promise<void> {
  const response = await page.goto(path);
  assertStaticHeaders(response, path);
  assertCsp(response, path);
}

test("every page (logged out and in) serves the required headers with zero CSP violations", async ({
  page,
}) => {
  const violations: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (CSP_VIOLATION_RE.test(msg.text())) violations.push(`${page.url()}: ${msg.text()}`);
  });

  // --- logged out ---
  await visit(page, "/");
  await visit(page, "/login");

  // --- register -> logged in ---
  const email = uniqueEmail("sechdr");
  const password = "correct-horse-battery-staple";
  await registerAndReachChat(page, email, password);
  await expect(page).toHaveURL(/\/chat$/);
  // The navigation above already happened; re-visit explicitly to capture & assert this
  // page's own top-level response headers (goto returns the navigation's own response).
  await visit(page, "/chat");
  await visit(page, "/account");

  // --- sign out -> logged out again ---
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await visit(page, "/login");

  // --- the forged-cookie 401 redirect path (T12.1.8) also renders through the same CSP ---
  await page.goto("/");
  await page.evaluate(() => {
    document.cookie = "__Host-secrag_session=not-a-real-session-id; Path=/; Secure; SameSite=Lax";
  });
  await visit(page, "/chat");
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });

  expect(violations, `CSP violations:\n${violations.join("\n")}`).toHaveLength(0);
});

test("a fresh login panel still gets the same headers (second vault-door open)", async ({
  page,
}) => {
  // A cheap second data point distinct from the first test's flow: opening the vault door
  // itself (client-side state change, no navigation) must not strip the headers that were
  // already set on the page's own top-level response.
  const response = await page.goto("/login");
  assertStaticHeaders(response, "/login");
  assertCsp(response, "/login");
  await openVaultDoor(page);
  await expect(page.getByPlaceholder("email")).toBeVisible();
});
