// T13.10.3 (DEBT-057): the custom 404 page, against the real built app
// (scripts/gate.sh's step_e2e). Playwright's default context here is `reducedMotion: "reduce"`
// (playwright.config.ts) -- every test below exercises the STATIC frame unless it opts out,
// which is deliberate: that is also the accessibility requirement (prefers-reduced-motion ->
// no motion at all).
import { expect, test } from "@playwright/test";

const UNKNOWN_PATH = "/this-room-does-not-exist-secrag-404-xyz";

test("an unknown route returns a real HTTP 404 and shows the vault 404 page", async ({ page }) => {
  const response = await page.goto(UNKNOWN_PATH);
  expect(response?.status()).toBe(404); // never a soft-404 200 (T13.10.2)

  await expect(page.getByRole("heading", { name: "This room isn't in the vault." })).toBeVisible();
  await expect(page.getByText("404")).toBeVisible();
  await expect(page.getByText("room not found")).toBeVisible();

  // Exactly one way out, and nothing else clickable on the page.
  const links = page.locator("a");
  await expect(links).toHaveCount(1);
  const button = page.getByRole("link", { name: "Back to login" });
  await expect(button).toBeVisible();
  await expect(page.getByRole("button")).toHaveCount(0);
});

test("no echo of the requested path anywhere on the page", async ({ page }) => {
  await page.goto(UNKNOWN_PATH);
  const bodyText = await page.locator("body").innerText();
  expect(bodyText).not.toContain(UNKNOWN_PATH);
  expect(bodyText).not.toContain("this-room-does-not-exist");
  // And no other link to anywhere else in the app (e.g. /chat) is offered either.
  await expect(page.getByRole("link", { name: /chat/i })).toHaveCount(0);
});

test("the button navigates to /login", async ({ page }) => {
  await page.goto(UNKNOWN_PATH);
  await page.getByRole("link", { name: "Back to login" }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("negative control: an unknown /api/... route stays a JSON 404, never this HTML page", async ({
  request,
}) => {
  const response = await request.get("/api/this-endpoint-does-not-exist-secrag-404-xyz");
  expect(response.status()).toBe(404);
  expect(response.headers()["content-type"]).toContain("application/json");
  const text = await response.text();
  expect(text).not.toContain("This room isn't in the vault.");
});

test("/admin is still a real route, never this 404 (today's behaviour unchanged)", async ({
  page,
}) => {
  // No session cookie at all: the UX-only middleware guard (lib/authGuard.ts) still redirects
  // /admin to /login exactly as before T13.10.1 -- it must NEVER become this 404 page. The
  // separate "logged-in but not an admin" case (its own SSR denied-placeholder, also
  // unaffected by this change) is already covered by e2e/admin.spec.ts.
  const response = await page.goto("/admin");
  expect(response?.status()).not.toBe(404);
  await expect(page).toHaveURL(/\/login$/);
});

test("renders with no horizontal scroll at a phone width (~390px)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(UNKNOWN_PATH);
  await expect(page.getByRole("link", { name: "Back to login" })).toBeVisible();
  const [scrollWidth, clientWidth] = await page.evaluate(() => [
    document.documentElement.scrollWidth,
    document.documentElement.clientWidth,
  ]);
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
});

test("the ASCII art is aria-hidden and the heading is a real, visible <h1>", async ({ page }) => {
  await page.goto(UNKNOWN_PATH);
  const art = page.locator("pre[aria-hidden='true']");
  await expect(art).toHaveCount(1);
  const heading = page.locator("h1");
  await expect(heading).toHaveCount(1);
  await expect(heading).toHaveText("This room isn't in the vault.");
});

test("under prefers-reduced-motion the art never changes (static frame)", async ({ page }) => {
  await page.goto(UNKNOWN_PATH);
  const art = page.locator("pre[aria-hidden='true']");
  const first = await art.innerHTML();
  await page.waitForTimeout(2_000); // longer than the 850ms frame interval
  const second = await art.innerHTML();
  expect(second).toBe(first);
});

test.describe("without reduced motion, the stickman actually animates", () => {
  test.use({ reducedMotion: "no-preference" });

  test("the art changes over time", async ({ page }) => {
    await page.goto(UNKNOWN_PATH);
    const art = page.locator("pre[aria-hidden='true']");
    const first = await art.innerHTML();
    await expect
      .poll(async () => (await art.innerHTML()) !== first, { timeout: 5_000 })
      .toBe(true);
  });
});

test("the login page button keeps getting visible focus (keyboard access)", async ({ page }) => {
  await page.goto(UNKNOWN_PATH);
  const button = page.getByRole("link", { name: "Back to login" });
  await button.focus();
  await expect(button).toBeFocused();
});
