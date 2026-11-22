// T13.12.6/T13.12.7 (13-J3b, DEBT-062): the REAL pipeline steps shown INSIDE the chat wake
// banner -- same HOLD technique as e2e/wakeup-slow.spec.ts (never faking the eventual
// answer: `page.route()` only delays the request a few seconds before `route.continue()`
// lets the REAL backend answer it for real). This proves the wiring end to end against the
// genuine SSE event sequence (conversation -> stage classifying -> stage retrieving ->
// step embed_query start/end -> [stage reranking] -> stage generating -> stage checking ->
// tokens -> done), not just the macro "waking"/"idle" state wakeup-slow.spec.ts already
// covers. The unit-level step-by-step transitions (embed current while embed_query runs,
// etc.) are exhaustively proven by lib/wakeSteps.test.ts (a pure function, no network) --
// this file deliberately does not try to catch that SAME transient mid-flight render over a
// real (locally fast, sub-second) Ollama embed call, which would be an inherently flaky
// assertion (Playwright polls on a ~100ms cadence; a real local embed can resolve well
// under that). What IS reliably, deterministically observable here: the step list's INITIAL
// state while the request is still held (before anything at all has arrived), and its FINAL
// state once the real answer starts streaming (every earlier step correctly "done", never
// stuck "pending").
import { expect, test } from "@playwright/test";

import { loginAndReachChat } from "./helpers.ts";

const SECURITY_QUESTION = "How do I prevent SQL injection?";
const WAKEUP_EMAIL = process.env.E2E_WAKEUP_EMAIL;
const WAKEUP_PASSWORD = process.env.E2E_ADMIN_PASSWORD;

test.skip(
  !WAKEUP_EMAIL || !WAKEUP_PASSWORD,
  "E2E_WAKEUP_EMAIL/E2E_ADMIN_PASSWORD not set -- only scripts/gate.sh's step_e2e sets these",
);

test("chat: the wake banner's pipeline steps start at 'Backend starting' and end with every earlier step done, never two banners, question sent once", async ({
  page,
}) => {
  await loginAndReachChat(page, WAKEUP_EMAIL as string, WAKEUP_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  let requests = 0;
  await page.route("**/api/chat/stream", async (route) => {
    requests += 1;
    // No `fulfill()`, ever -- the activator-hold case produces NO response of any kind
    // until the replica is ready (same technique as wakeup-slow.spec.ts). 6.5s comfortably
    // straddles the ~3s pending threshold either side.
    await new Promise((resolve) => setTimeout(resolve, 6_500));
    return route.continue();
  });

  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  const banner = page.getByTestId("wake-banner");
  await expect(banner).toBeVisible({ timeout: 4_000 });
  await expect(banner).toHaveAttribute("aria-live", "polite");

  // Still well within the 6.5s hold: nothing has arrived from the (real) backend at all --
  // "Backend starting" is the only step shown as current, everything else still pending.
  const steps = page.getByTestId("wake-banner-steps");
  await expect(steps).toBeVisible();
  await expect(page.getByTestId("wake-banner-step-backend")).toHaveAttribute("data-status", "current");
  for (const id of ["embed", "retrieving", "reranking", "generating", "checking"]) {
    await expect(page.getByTestId(`wake-banner-step-${id}`)).toHaveAttribute("data-status", "pending");
  }

  // The real backend finally answers for real, exactly once -- the banner (and its step
  // list with it) disappears with the first streamed token, well before the full answer.
  await expect(banner).toHaveCount(0, { timeout: 60_000 });
  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  await expect(thread.getByText(SECURITY_QUESTION)).toHaveCount(1);
  expect(requests).toBe(1); // the question was sent exactly once -- this feature never resends
});

test("negative control: a fast chat response never shows the waking banner or its steps", async ({ page }) => {
  await loginAndReachChat(page, WAKEUP_EMAIL as string, WAKEUP_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  // Checked only AFTER the answer is in (never mid-flight): proves the banner (and its
  // steps) never appeared at any point during this fast turn, not just that it is gone now.
  await expect(page.getByTestId("wake-banner")).toHaveCount(0);
  await expect(page.getByTestId("wake-banner-steps")).toHaveCount(0);
});
