// T13.2.10: the real admin dashboard, end to end -- login as the e2e-seeded admin account
// (scripts/gate.sh's `e2e_seed_admin`, T13.2.10), enrol + confirm + step up MFA for REAL
// through the UI (a real TOTP code computed from the real backend-issued secret, via
// `e2e/totp.ts` -- no backend test hook, no hand-typed secret), then every dashboard card
// renders and the erasure panel (incl. the "Erase an account" form, T13.1.8/DEBT-002)
// works against a real backend.
import { expect, test } from "@playwright/test";

import { loginAndReachChat } from "./helpers.ts";
import { totpCode } from "./totp.ts";

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL;
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD;
// DA-13-C-1: seeded directly (scripts/gate.sh's e2e_seed_admin --role user), never
// registered through the UI -- see that function's own comment for why (signup-velocity
// budget).
const ERASE_TARGET_EMAIL = process.env.E2E_ERASE_TARGET_EMAIL;

test.skip(
  !ADMIN_EMAIL || !ADMIN_PASSWORD || !ERASE_TARGET_EMAIL,
  "E2E_ADMIN_EMAIL/E2E_ADMIN_PASSWORD/E2E_ERASE_TARGET_EMAIL not set -- only" +
    " scripts/gate.sh's step_e2e sets these",
);

async function enrolAndStepUpMfa(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/account/security");
  await page.getByRole("button", { name: "Start enrolment" }).click();

  const secretText = await page.getByText(/^Secret: /).textContent();
  const secret = secretText?.replace("Secret: ", "").trim();
  if (!secret) throw new Error("MFA secret was not rendered");

  await page.getByLabel(/6-digit code your app shows/).fill(totpCode(secret));
  await page.getByRole("button", { name: "CONFIRM" }).click();
  await expect(page.getByText("Two-factor authentication is enabled.")).toBeVisible();

  await page.getByLabel(/Step up this session now/).fill(totpCode(secret));
  await page.getByRole("button", { name: "Step up" }).click();
  await expect(page.getByText("MFA verified.")).toBeVisible();
}

test("an admin can enrol MFA, step up, and see the full dashboard", async ({ page }) => {
  await loginAndReachChat(page, ADMIN_EMAIL as string, ADMIN_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  await enrolAndStepUpMfa(page);

  await page.goto("/admin");
  await expect(page.getByText("Admin dashboard")).toBeVisible();

  // Every card (T13.2.10's own "every card" requirement).
  for (const testId of [
    "admin-overview-panel",
    "admin-feature-cards",
    "admin-latency-panel",
    "admin-security-panel",
    "admin-quality-panel",
    "admin-erasure-panel",
  ]) {
    await expect(page.getByTestId(testId)).toBeVisible();
  }

  // At least the admin-api feature itself is rendered as a card (the registry is real).
  await expect(page.getByTestId("feature-card-admin-api")).toBeVisible();
});

test("the admin can erase another account's data through the erasure panel", async ({ page }) => {
  await loginAndReachChat(page, ADMIN_EMAIL as string, ADMIN_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);
  await enrolAndStepUpMfa(page);

  await page.goto("/admin");
  const form = page.getByTestId("erase-account-form");
  await expect(form).toBeVisible();
  await form.getByLabel("Account email").fill(ERASE_TARGET_EMAIL as string);
  await form.getByLabel("Type ERASE to confirm").fill("ERASE");
  await form.getByRole("button", { name: "Erase account" }).click();

  // The response never echoes the submitted email -- only an opaque request id + detail.
  await expect(form.getByText(/request /)).toBeVisible();
  await expect(page.getByText(ERASE_TARGET_EMAIL as string)).toHaveCount(0);

  // The erasure panel's table now lists at least one open request.
  await expect(page.getByTestId("admin-erasure-panel").locator("tbody tr").first()).toBeVisible();
});
