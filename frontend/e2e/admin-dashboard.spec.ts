// T13.2.10: the real admin dashboard, end to end -- login as the e2e-seeded admin account
// (scripts/gate.sh's `e2e_seed_admin`, T13.2.10), enrol + confirm + step up MFA for REAL
// through the UI (a real TOTP code computed from the real backend-issued secret, via
// `e2e/totp.ts` -- no backend test hook, no hand-typed secret), then every dashboard card
// renders and the erasure panel (incl. the "Erase an account" form, T13.1.8/DEBT-002)
// works against a real backend.
import { expect, test } from "@playwright/test";

import { enrolAndStepUpMfa, loginAndReachChat, stepUpOnlyMfa } from "./helpers.ts";

// T13.7.5/DEBT-051: set by the first test below (it enrols ADMIN_EMAIL for real) so the
// step-up-only test further down can prove a NEW session for the SAME, already-enrolled
// account without ever re-enrolling. Tests in this file run sequentially (no
// `fullyParallel`, playwright.config.ts) -- the assignment always happens before the read.
let adminSecret: string | undefined;

const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL;
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD;
// DEBT-051/DEC-041 (13-F1): the erasure test below gets its OWN seeded admin -- see this
// file's `e2e_seed_admin`/`E2E_ADMIN2_EMAIL` comment in scripts/gate.sh for why (reusing
// ADMIN_EMAIL's account would hit the re-enrolment proof check this block adds, since
// `enrolAndStepUpMfa` always (re-)enrols from scratch through the UI).
const ADMIN2_EMAIL = process.env.E2E_ADMIN2_EMAIL;
// DA-13-C-1: seeded directly (scripts/gate.sh's e2e_seed_admin --role user), never
// registered through the UI -- see that function's own comment for why (signup-velocity
// budget).
const ERASE_TARGET_EMAIL = process.env.E2E_ERASE_TARGET_EMAIL;
// T13.7.5/DEBT-051: same reasoning as ERASE_TARGET_EMAIL above -- seeded directly (role
// "user") rather than self-registered, so the "a plain user never sees Admin panel" check
// below never adds to the real `/auth/register` signup-velocity budget this step's other
// tests already use (found blocking an unrelated test for real, scripts/gate.sh's own
// E2E_PLAIN_USER_EMAIL comment has the detail).
const PLAIN_USER_EMAIL = process.env.E2E_PLAIN_USER_EMAIL;

test.skip(
  !ADMIN_EMAIL || !ADMIN2_EMAIL || !ADMIN_PASSWORD || !ERASE_TARGET_EMAIL || !PLAIN_USER_EMAIL,
  "E2E_ADMIN_EMAIL/E2E_ADMIN2_EMAIL/E2E_ADMIN_PASSWORD/E2E_ERASE_TARGET_EMAIL/" +
    "E2E_PLAIN_USER_EMAIL not set -- only scripts/gate.sh's step_e2e sets these",
);

test("an admin can enrol MFA, step up, and see the full dashboard", async ({ page }) => {
  await loginAndReachChat(page, ADMIN_EMAIL as string, ADMIN_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  adminSecret = await enrolAndStepUpMfa(page);

  await page.goto("/admin");
  await expect(page.getByText("Admin dashboard")).toBeVisible();

  // T13.5.2: a freshly seeded e2e stack has nothing to alert on -- no banner at all.
  await expect(page.getByTestId("admin-alert-banner")).toHaveCount(0);

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
  await loginAndReachChat(page, ADMIN2_EMAIL as string, ADMIN_PASSWORD as string);
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

// T13.7.5/DEBT-051: the real step-up-only e2e path the backend fix (13-F1) was missing --
// an already-enrolled admin logging into a BRAND NEW session (its own browser context, so
// no cookie survives from the first test's session) must see only the step-up form, step up
// by pressing Enter (no button click), and reach the Admin panel link -- never touching
// enrolment again. The same page also proves a plain user never sees that link, and that
// re-enrolling without a code is rejected in the UI with a readable message.
test("an already-enrolled admin steps up in a brand-new session without re-enrolling", async ({
  page,
  browser,
}) => {
  const context = await browser.newContext();
  const freshPage = await context.newPage();
  try {
    await loginAndReachChat(freshPage, ADMIN_EMAIL as string, ADMIN_PASSWORD as string);
    await expect(freshPage).toHaveURL(/\/chat$/);

    await stepUpOnlyMfa(freshPage, adminSecret as string);
    // Still the same page as stepUpOnlyMfa left it (no reload): the Admin panel link
    // appears right after a successful step-up, same render, no extra navigation.
    await expect(freshPage.getByRole("link", { name: "Admin panel" })).toBeVisible();

    // Re-enrolling without a code is rejected in the UI (403 shown nicely, no QR code
    // appears, the account stays untouched) -- the separate, warned action, not the
    // default view. Submitted by pressing Enter in the (empty) code field, not a button
    // click, proving the re-enrol form is a real <form onSubmit> too.
    await freshPage
      .getByRole("button", { name: "Replace my two-factor authentication" })
      .click();
    await freshPage.getByLabel(/Current 6-digit code or a recovery code/).press("Enter");
    await expect(freshPage.getByText(/didn't match/)).toBeVisible();
    await expect(freshPage.getByTestId("mfa-qr-code")).toHaveCount(0);
  } finally {
    await context.close();
  }

  // A plain (non-admin) user, seeded with role "user" and self-enrolled -- never sees the
  // link.
  await loginAndReachChat(page, PLAIN_USER_EMAIL as string, ADMIN_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);
  await enrolAndStepUpMfa(page);
  // Same page `enrolAndStepUpMfa` left it on (no reload): a real step-up just completed,
  // so this genuinely proves the probe returned 403 for a non-admin, not merely "nobody
  // checked yet".
  await expect(page.getByRole("link", { name: "Admin panel" })).toHaveCount(0);
});
