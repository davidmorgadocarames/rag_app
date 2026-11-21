// T13.11.5/T13.11.6 (13-J2, DEBT-060): the gap e2e/wakeup.spec.ts's own scenarios cannot
// reach -- a cold start that is HELD, not refused. On Azure the Container Apps activator
// queues the request during a scale-from-zero instead of answering with any error at all, so
// `lib/wakeUp.ts`'s retry policy (which only reacts to a FAILED attempt) never fires. Proven
// locally by the orchestrator (2026-11-21): a backend that refuses the connection shows the
// 13-H banner after 3 s; a backend that merely HOLDS the request for 20 s shows nothing but
// the button's own "…" -- this is what the user actually saw on Azure.
//
// `page.route()` reproduces the HOLD by delaying the real request a few seconds before
// `route.continue()` lets it through unmodified -- never faking a response body -- so the
// eventual, single answer is the real backend's own, exactly like e2e/wakeup.spec.ts's own
// pattern.
import { expect, test } from "@playwright/test";

import { loginAndReachChat } from "./helpers.ts";

const SECURITY_QUESTION = "How do I prevent SQL injection?";
const WAKEUP_EMAIL = process.env.E2E_WAKEUP_EMAIL;
const WAKEUP_PASSWORD = process.env.E2E_ADMIN_PASSWORD;

test.skip(
  !WAKEUP_EMAIL || !WAKEUP_PASSWORD,
  "E2E_WAKEUP_EMAIL/E2E_ADMIN_PASSWORD not set -- only scripts/gate.sh's step_e2e sets these",
);

test("chat: a slow (HELD, never refused) cold start shows the waking banner after ~3s, then the real answer arrives exactly once", async ({
  page,
}) => {
  await loginAndReachChat(page, WAKEUP_EMAIL as string, WAKEUP_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  await page.route("**/api/chat/stream", async (route) => {
    // No `fulfill()`, ever -- the activator-hold case produces NO response of any kind
    // until the replica is ready. 6.5s comfortably straddles the ~3s threshold either side.
    await new Promise((resolve) => setTimeout(resolve, 6_500));
    return route.continue();
  });

  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  // The question appears exactly once immediately -- this block never retries/resends.
  await expect(thread.getByText(SECURITY_QUESTION)).toHaveCount(1);

  const banner = page.getByTestId("wake-banner");
  // Not before the threshold: the request is still held, well under 3s in.
  await page.waitForTimeout(1_500);
  await expect(banner).toHaveCount(0);

  // Visible once the ~3s threshold has passed, while the request is STILL held (no error
  // was ever thrown -- this is the purely-visual timer, not the 13-H retry banner).
  await expect(banner).toBeVisible({ timeout: 4_000 });
  await expect(banner).toHaveText(/Waking the server up/);
  await expect(banner).toHaveAttribute("aria-live", "polite");
  // No Retry button: purely visual, never the retry-driven "failed" state -- confirms this
  // is lib/pendingWake.ts's timer, not a real wake-retry exhaustion.
  await expect(page.getByTestId("wake-banner-retry")).toHaveCount(0);

  // The real backend finally answers, exactly once, and the banner disappears with the
  // first streamed byte (well before the full answer finishes).
  await expect(banner).toHaveCount(0, { timeout: 60_000 });
  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  await expect(thread.getByText(SECURITY_QUESTION)).toHaveCount(1);
});

test("negative control: a fast chat response never shows the waking banner", async ({ page }) => {
  await loginAndReachChat(page, WAKEUP_EMAIL as string, WAKEUP_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  // Checked only AFTER the answer is in (never mid-flight): proves the banner never
  // appeared at any point during this fast turn, not just that it is gone now.
  await expect(page.getByTestId("wake-banner")).toHaveCount(0);
});
