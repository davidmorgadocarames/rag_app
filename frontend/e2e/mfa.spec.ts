// T12.3.5 (DEC-024 = B): proves the QR code actually renders client-side on
// `/account/security` once enrolment starts, in a real browser -- `components/QrCode.tsx`
// is otherwise only covered by `node --test` at the pure-render-logic level
// (`components/QrCode.test.tsx`), which cannot see the real `otpauth://` URI returned by
// the backend or a mounted DOM.
import { expect, test } from "@playwright/test";

import { registerAndReachChat, uniqueEmail } from "./helpers.ts";
import { totpCode } from "./totp.ts";

test("starting MFA enrolment renders a QR code, and Enter submits the confirm/step-up codes", async ({
  page,
}) => {
  const email = uniqueEmail("mfa");
  const password = "correct-horse-battery-staple";
  await registerAndReachChat(page, email, password);
  await expect(page).toHaveURL(/\/chat$/);

  await page.goto("/account/security");
  await page.getByRole("button", { name: "Start enrolment" }).click();

  const qr = page.getByTestId("mfa-qr-code");
  await expect(qr).toBeVisible();
  // A genuine QR render, not a placeholder: an <svg> with many filled modules (dark rects),
  // not just an empty shell.
  const svg = qr.locator("svg");
  await expect(svg).toHaveAttribute("role", "img");
  const rectCount = await svg.locator("rect").count();
  expect(rectCount).toBeGreaterThan(50);

  // The secret/URI manual-entry fallback is still shown alongside the QR code.
  await expect(page.getByText(/^Secret: /)).toBeVisible();
  await expect(page.getByText(/^URI: otpauth:\/\//)).toBeVisible();

  const secretText = await page.getByText(/^Secret: /).textContent();
  const secret = secretText?.replace("Secret: ", "").trim();
  if (!secret) throw new Error("MFA secret was not rendered");

  // T13.7.4: the code input sits inside a real <form onSubmit> on both the enrol-confirm
  // and the step-up form -- pressing Enter submits it, no button click needed.
  const confirmInput = page.getByLabel(/6-digit code your app shows/);
  await confirmInput.fill(totpCode(secret));
  await confirmInput.press("Enter");
  await expect(page.getByText("Two-factor authentication is enabled.")).toBeVisible();

  const stepUpInput = page.getByLabel(/Step up this session now/);
  await stepUpInput.fill(totpCode(secret));
  await stepUpInput.press("Enter");
  await expect(page.getByText("MFA verified.")).toBeVisible();
});
