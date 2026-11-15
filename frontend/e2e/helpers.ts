// Shared helpers for the Playwright specs under this directory (T12.1.10, T12.2.3/T12.2.5).
// Each spec is started by its own `scripts/gate.sh` step against the SAME native
// backend+frontend pair (`step_e2e`/`step_security-headers`/`step_zap-baseline` all reuse
// `e2e_start_backend`/`e2e_start_frontend`) -- never run concurrently, so sharing these pure
// UI-driving helpers across spec files is safe.
import { expect, type Page } from "@playwright/test";

import { totpCode } from "./totp.ts";

export function uniqueEmail(prefix = "e2e"): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}@example.test`;
}

export async function openVaultDoor(page: Page): Promise<void> {
  // `reducedMotion: "reduce"` (playwright.config.ts) makes VaultDoor skip straight to the
  // open state synchronously, so the panel's `pointer-events` switches to `auto` right
  // away -- `.click()` (not `.waitFor("visible")`, which Playwright considers true even
  // while `pointer-events: none`/`opacity-0`, proven wrong the hard way: a real run got
  // stuck here for the full 60s test timeout before this fix) actually waits for that.
  await page.getByRole("button", { name: "ABRIR" }).click();
  await page.getByPlaceholder("email").click();
}

export async function registerAndReachChat(
  page: Page,
  email: string,
  password: string,
): Promise<void> {
  // T12.4.4: register no longer opens a session -- LoginPanel shows a "check your email"
  // message and switches back to the login form; every caller of this shared helper just
  // wants a LOGGED-IN ordinary user (ENV=dev: REQUIRE_EMAIL_VERIFICATION defaults to false,
  // so an unverified account can still log in -- the dedicated Mailpit verification flow is
  // exercised by auth.spec.ts's own test, not every caller here).
  await page.goto("/login");
  await openVaultDoor(page);
  await page.getByText("Need access? Register").click();
  await page.getByPlaceholder("email").fill(email);
  await page.getByPlaceholder(/password/).fill(password);
  await page.getByRole("button", { name: "REGISTER" }).click();
  // Wait for LoginPanel's post-register message BEFORE refilling: it switches mode and
  // clears the password field as part of the SAME state update -- filling first and
  // waiting second would race that clear and silently submit an empty password (the
  // browser's own `required` validation then blocks the submit with no network request
  // at all, found running this helper for real).
  await expect(page.getByText(/you'll receive an email/i)).toBeVisible();
  await page.getByPlaceholder("email").fill(email);
  await page.getByPlaceholder(/password/).fill(password);
  await page.getByRole("button", { name: "ENTER" }).click();
}

/** Logs in an ALREADY-EXISTING account (T13.2.10: the e2e admin, seeded directly in the
 * database by `scripts/gate.sh`'s `e2e_seed_admin` -- never through a route, T12.3.3) --
 * `registerAndReachChat` above always registers a brand new one first. */
export async function loginAndReachChat(
  page: Page,
  email: string,
  password: string,
): Promise<void> {
  await page.goto("/login");
  await openVaultDoor(page);
  await page.getByPlaceholder("email").fill(email);
  await page.getByPlaceholder(/password/).fill(password);
  await page.getByRole("button", { name: "ENTER" }).click();
}

/** T13.2.10: enrol + confirm + step up MFA for real through the UI (a real TOTP code
 * from the real backend-issued secret, `e2e/totp.ts`). Shared by admin-dashboard.spec.ts
 * and security-headers.spec.ts's `/admin` walk (DEC-036). */
export async function enrolAndStepUpMfa(page: Page): Promise<void> {
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
