// T12.3.5 (DEC-024 = B): proves the QR code actually renders client-side on
// `/account/security` once enrolment starts, in a real browser -- `components/QrCode.tsx`
// is otherwise only covered by `node --test` at the pure-render-logic level
// (`components/QrCode.test.tsx`), which cannot see the real `otpauth://` URI returned by
// the backend or a mounted DOM.
import { expect, test } from "@playwright/test";

import { registerAndReachChat, uniqueEmail } from "./helpers.ts";

test("starting MFA enrolment renders a QR code built from the real provisioning URI", async ({
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
});
