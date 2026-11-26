// T14.4.1/T14.4.2/T14.4.3 (block 14-D2): the chat UI's visible "plan" panel, through a real
// chat turn (register -> one streamed security question, same backend+frontend pair every
// other spec in this directory uses, scripts/gate.sh's `e2e` step; --full only). Proves, in
// a real browser, what lib/planSteps.test.ts can only prove against synthetic events: the
// REAL SSE "step" events (T14.4.1) actually drive the panel end to end -- collapsed by
// default, a live one-line status, expandable via a real keyboard-operable <button>, and
// the full ordered transcript once expanded.
import { expect, test } from "@playwright/test";

import { registerAndReachChat, uniqueEmail } from "./helpers.ts";

const SECURITY_QUESTION = "How do I prevent SQL injection?";

test("chat: the plan panel is collapsed by default, shows a live status, and expands via keyboard to the full ordered transcript", async ({
  page,
}) => {
  const email = uniqueEmail("e2e-plan");
  const password = "correct-horse-battery-staple";
  await registerAndReachChat(page, email, password);
  await expect(page).toHaveURL(/\/chat$/);

  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  // The panel appears once the first plan step ("Understood: …") arrives, well before the
  // answer finishes -- collapsed by default (no entry list visible yet).
  const panel = page.getByTestId("plan-panel");
  await expect(panel).toBeVisible({ timeout: 20_000 });
  const toggle = page.getByTestId("plan-panel-toggle");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByTestId("plan-panel-list")).toHaveCount(0);

  // Wait for the full answer (proves the real pipeline ran end to end): the panel's last
  // plan step for a security question is always "Writing answer".
  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("plan-panel-current")).toHaveText("Writing answer");

  // Keyboard-accessible: a real <button>, reached by Tab, activated by Enter (never only a
  // mouse click) -- toggles the full transcript open, aria-expanded flips to "true".
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const list = page.getByTestId("plan-panel-list");
  await expect(list).toBeVisible();

  // Every plan step this question's real router/retrieval path produces, in order, each
  // with its own real (non-empty) detail text -- never the literal placeholder/no steps.
  // The exact `standalone_query` wording is the real LLM's own rewrite (never pinned
  // verbatim here -- that is `eval/golden_set_v2*`'s job); only the FIXED, code-controlled
  // shape of each label is asserted.
  await expect(page.getByTestId("plan-panel-entry-understood")).toHaveText(/^Understood: security\/en$/);
  await expect(page.getByTestId("plan-panel-entry-searching")).toHaveText(/^Searching: .+$/);
  await expect(page.getByTestId("plan-panel-entry-found_sources")).toHaveText(/^Found \d+ sources$/);
  await expect(page.getByTestId("plan-panel-entry-writing_answer")).toHaveText("Writing answer");

  // Collapsing again via the SAME keyboard-operable control.
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(list).toHaveCount(0);
});
