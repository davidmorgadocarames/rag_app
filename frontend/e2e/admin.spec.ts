// T12.3.6 case (5)/(6), in a real browser: the frontend never decides authorisation, and
// client-state tampering (localStorage, a forged DOM flag) buys nothing -- /admin always
// re-checks server-side (app/admin/page.tsx's SSR role+MFA check, which calls the real
// backend through the same BFF proxy every other route uses). An ordinary, non-admin user
// can never reach admin content, with or without tampering.
//
// Written in 12b-A; NOT run this session (PC constraint, no Playwright/Ollama) -- phase 2.
import { expect, test } from "@playwright/test";

import { registerAndReachChat, uniqueEmail } from "./helpers.ts";

test("an ordinary user visiting /admin sees the denied placeholder, not admin content", async ({
  page,
}) => {
  const email = uniqueEmail();
  const password = "correct-horse-battery-staple";
  await registerAndReachChat(page, email, password);
  await expect(page).toHaveURL(/\/chat$/);

  await page.goto("/admin");
  await expect(page.getByText("You do not have access to this page.")).toBeVisible();
});

test("tampering with localStorage/DOM before visiting /admin has no effect", async ({ page }) => {
  const email = uniqueEmail();
  const password = "correct-horse-battery-staple";
  await registerAndReachChat(page, email, password);
  await expect(page).toHaveURL(/\/chat$/);

  // Whatever a compromised/forged client state might claim, the page's own SSR check calls
  // the real backend -- these values are never read by app/admin/page.tsx at all.
  await page.evaluate(() => {
    window.localStorage.setItem("role", "admin");
    window.localStorage.setItem("isAdmin", "true");
    document.documentElement.dataset.role = "admin";
  });

  await page.goto("/admin");
  await expect(page.getByText("You do not have access to this page.")).toBeVisible();
});
