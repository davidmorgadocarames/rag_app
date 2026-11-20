// T13.9.2 (DEBT-056, DEC-048): the wake-up UX end to end, in a real browser, against the gate's
// real backend+frontend (scripts/gate.sh's step_e2e) -- `page.route()` plays the role of "an
// injected slow/503 backend" by intercepting the BROWSER's own request to the BFF route and
// answering with exactly what lib/bffProxy.ts itself returns for a cold-start-shaped failure
// (502 "upstream unreachable" / "upstream timed out"), then letting the REAL request through
// once the scenario is done -- so the eventual success/error is the real backend's own, never
// faked. See lib/wakeUp.ts's module comment for the safe/unsafe policy these scenarios prove.
//
// E2E_WAKEUP_EMAIL (scripts/gate.sh's own e2e_seed_admin): seeded directly, never registered
// through the UI -- auth/admin/mfa.spec.ts's own throwaway registrations already leave little
// headroom in rag_app.risk.SignupTracker's 5-signups/hour/IP budget (gate.sh's own comment on
// E2E_PLAIN_USER_EMAIL has the detail: a 6th registration in one run tripped it for real); a
// DIFFERENT seeded account than E2E_PLAIN_USER_EMAIL so a parallel admin-dashboard.spec.ts run
// (which enrols/steps up MFA on that one) never interleaves with this file's own traffic.
import { expect, test } from "@playwright/test";

import { loginAndReachChat, openVaultDoor, uniqueEmail } from "./helpers.ts";

const SECURITY_QUESTION = "How do I prevent SQL injection?";
const WAKEUP_EMAIL = process.env.E2E_WAKEUP_EMAIL;
const WAKEUP_PASSWORD = process.env.E2E_ADMIN_PASSWORD;

test.skip(
  !WAKEUP_EMAIL || !WAKEUP_PASSWORD,
  "E2E_WAKEUP_EMAIL/E2E_ADMIN_PASSWORD not set -- only scripts/gate.sh's step_e2e sets these",
);

function upstreamUnreachable() {
  return {
    status: 502,
    contentType: "application/json",
    body: JSON.stringify({ detail: "upstream unreachable" }),
  };
}

function upstreamTimedOut() {
  return {
    status: 502,
    contentType: "application/json",
    body: JSON.stringify({ detail: "upstream timed out" }),
  };
}

test("chat: a connect-refused cold start retries automatically and the question resumes, never duplicated", async ({
  page,
}) => {
  await loginAndReachChat(page, WAKEUP_EMAIL as string, WAKEUP_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  let attempts = 0;
  await page.route("**/api/chat/stream", async (route) => {
    attempts += 1;
    if (attempts <= 2) return route.fulfill(upstreamUnreachable());
    return route.continue();
  });

  // Scoped to the thread (not the whole page): the sidebar's own conversation list reuses
  // the first message as its title (backend/src/rag_app/api/conversations.py::_title_for),
  // so an EARLIER test's conversation on this same shared account would otherwise also
  // match the exact same question text there.
  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  // The question appears exactly once, immediately -- never lost, never duplicated by the
  // two forced retries that follow.
  await expect(thread.getByText(SECURITY_QUESTION)).toHaveCount(1);

  const banner = page.getByTestId("wake-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toHaveText(/Waking the server up/);
  await expect(banner).toHaveAttribute("aria-live", "polite");

  // The real, grounded answer streams in once the (real) backend actually answers.
  await expect(banner).toHaveCount(0, { timeout: 60_000 });
  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  expect(attempts).toBe(3);
});

test("chat: an ambiguous cold-start failure is never auto-resubmitted -- Retry sends it exactly once", async ({
  page,
}) => {
  await loginAndReachChat(page, WAKEUP_EMAIL as string, WAKEUP_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  let attempts = 0;
  await page.route("**/api/chat/stream", async (route) => {
    attempts += 1;
    // The FIRST attempt (the user's own click) may already have reached the backend --
    // never resent automatically. Only the SECOND attempt (the explicit Retry click below)
    // is allowed through to the real backend.
    if (attempts === 1) return route.fulfill(upstreamTimedOut());
    return route.continue();
  });

  const thread = page.getByTestId("chat-thread"); // see the previous test's comment on why
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  const banner = page.getByTestId("wake-banner");
  await expect(banner).toBeVisible();
  const retryButton = page.getByTestId("wake-banner-retry");
  await expect(retryButton).toBeVisible();
  // Give the (fake) network a moment: if the question were ever auto-resubmitted, a second
  // intercepted call would already have landed by now.
  await page.waitForTimeout(500);
  expect(attempts).toBe(1);

  await retryButton.click();
  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  expect(attempts).toBe(2);
  // Still exactly one user bubble -- the Retry click never re-adds the question to the thread.
  await expect(thread.getByText(SECURITY_QUESTION)).toHaveCount(1);
});

test("login: a connect-refused cold start retries automatically and still signs in", async ({
  page,
}) => {
  await page.goto("/login");
  await openVaultDoor(page);

  let attempts = 0;
  await page.route("**/api/auth/login", async (route) => {
    attempts += 1;
    if (attempts <= 2) return route.fulfill(upstreamUnreachable());
    return route.continue();
  });

  await page.getByPlaceholder("email").fill(WAKEUP_EMAIL as string);
  await page.getByPlaceholder(/password/).fill(WAKEUP_PASSWORD as string);
  await page.getByRole("button", { name: "ENTER" }).click();

  await expect(page.getByTestId("wake-banner")).toBeVisible();
  await expect(page).toHaveURL(/\/chat$/, { timeout: 30_000 });
  expect(attempts).toBe(3);
});

test("negative control: a real wrong-password 401 is shown as an ordinary error, never as waking up", async ({
  page,
}) => {
  // A never-registered address gets the exact same 401 "invalid credentials" as a real
  // account with the wrong password (anti-enumeration, backend/src/rag_app/api/auth.py) --
  // no seeded account, no registration needed for this check at all.
  await page.goto("/login");
  await openVaultDoor(page);
  await page.getByPlaceholder("email").fill(uniqueEmail("wake-negative"));
  await page.getByPlaceholder(/password/).fill("definitely-the-wrong-password");
  await page.getByRole("button", { name: "ENTER" }).click();

  await expect(page.getByText("invalid credentials")).toBeVisible();
  // Never shown as "waking up": a real 401 is classified "none" and never reaches the
  // wake-up banner at all.
  await expect(page.getByTestId("wake-banner")).toHaveCount(0);
});
