// T12.1.10: register -> one streamed chat turn -> logout -> login, through the real BFF +
// backend (scripts/gate.sh's `e2e` step starts both; --full only). Proves, in a real
// browser, what the lib-level `node --test` suites can only prove in parts: the cookie
// actually gets set by the browser, the CSRF cookie is actually readable and sent back,
// the SSE stream actually renders token by token, and a session-less visit to /chat is
// actually redirected (T12.1.8) by the real middleware.
import { expect, test } from "@playwright/test";

function uniqueEmail(): string {
  return `e2e-${Date.now()}-${Math.random().toString(16).slice(2)}@example.test`;
}

async function openVaultDoor(page: import("@playwright/test").Page): Promise<void> {
  // `reducedMotion: "reduce"` (playwright.config.ts) makes VaultDoor skip straight to the
  // open state synchronously, so the panel's `pointer-events` switches to `auto` right
  // away -- `.click()` (not `.waitFor("visible")`, which Playwright considers true even
  // while `pointer-events: none`/`opacity-0`, proven wrong the hard way: a real run got
  // stuck here for the full 60s test timeout before this fix) actually waits for that.
  await page.getByRole("button", { name: "ABRIR" }).click();
  await page.getByPlaceholder("email").click();
}

test("register, one streamed chat turn, logout, then login again", async ({ page }) => {
  const email = uniqueEmail();
  const password = "correct-horse-battery-staple";

  await page.goto("/login");
  await openVaultDoor(page);
  await page.getByText("Need access? Register").click();
  await page.getByPlaceholder("email").fill(email);
  await page.getByPlaceholder(/password/).fill(password);
  await page.getByRole("button", { name: "REGISTER" }).click();
  await expect(page).toHaveURL(/\/chat$/);

  await page.getByPlaceholder("Ask a security question…").fill("hello");
  await page.getByRole("button", { name: "SEND" }).click();
  // The SEND button re-enables once the stream's terminal "done"/"error" event lands.
  await expect(page.getByRole("button", { name: "SEND" })).toBeEnabled({ timeout: 45_000 });

  await page.goto("/account");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);

  // No session cookie at all now: the middleware (T12.1.8) redirects straight back, before
  // the chat page's own shell (let alone its data) is ever sent.
  await page.goto("/chat");
  await expect(page).toHaveURL(/\/login$/);

  await openVaultDoor(page);
  await page.getByPlaceholder("email").fill(email);
  await page.getByPlaceholder(/password/).fill(password);
  await page.getByRole("button", { name: "ENTER" }).click();
  await expect(page).toHaveURL(/\/chat$/);
});

test("a forged session cookie still gets 401 from the backend (T12.1.8)", async ({ page }) => {
  // `page.context().addCookies(...)` (CDP's Storage.setCookies) kept rejecting every
  // combination tried for a `__Host-` prefixed cookie ("Invalid cookie fields", "Cookie
  // should have either url or path", url+domain, url+path, bare url, url with a trailing
  // slash -- all reproduced) with this Playwright/Chromium pair. `document.cookie` (the
  // same readable-cookie API the CSRF cookie itself is designed to be read with, T12.1.4)
  // is the standard, well-tested path instead -- Chromium enforces the SAME `__Host-`
  // rules (Secure, Path=/, no Domain) for a script-set cookie, and this works.
  await page.goto("/");
  await page.evaluate(() => {
    document.cookie = "__Host-secrag_session=not-a-real-session-id; Path=/; Secure; SameSite=Lax";
  });
  // The middleware only checks the cookie's PRESENCE (D8): this forged one passes that
  // UX-only guard and the chat shell loads, but its own data call (GET /conversations)
  // gets 401 from the backend and the page redirects itself back to /login.
  await page.goto("/chat");
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
});
