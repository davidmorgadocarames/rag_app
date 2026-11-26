// T14.4.1/T14.4.2/T14.4.3 (block 14-D2): the chat UI's visible "plan" panel, through a real
// chat turn (login -> one streamed security question, same backend+frontend pair every
// other spec in this directory uses, scripts/gate.sh's `e2e` step; --full only). Proves, in
// a real browser, what lib/planSteps.test.ts can only prove against synthetic events: the
// REAL SSE "step" events (T14.4.1) actually drive the panel end to end -- collapsed by
// default, a live one-line status, expandable via a real keyboard-operable <button>, and
// the full ordered transcript once expanded.
//
// E2E_PLAN_PANEL_EMAIL (scripts/gate.sh's own e2e_seed_admin): seeded directly, never
// registered through the UI (14-D2-fix: this test's own intent is "does the plan panel
// render during a real chat turn", not "does registration work" -- auth.spec.ts already
// proves registration end to end). 14-D2's original version used `registerAndReachChat`,
// a real `/auth/register` call -- the 5th real UI registration sharing this gate step's one
// backend process, which tipped `rag_app.risk.SignupTracker`'s already-documented
// 5-signups/hour/IP budget (scripts/gate.sh's E2E_PLAN_PANEL_EMAIL comment has the detail)
// and deterministically 403-ed an unrelated test, auth.spec.ts's Mailpit-verify flow,
// reproduced 4/4 times and root-caused running `gate.sh --only e2e`.
import { expect, test } from "@playwright/test";

import { loginAndReachChat } from "./helpers.ts";

const SECURITY_QUESTION = "How do I prevent SQL injection?";
const PLAN_PANEL_EMAIL = process.env.E2E_PLAN_PANEL_EMAIL;
const PLAN_PANEL_PASSWORD = process.env.E2E_ADMIN_PASSWORD;

test.skip(
  !PLAN_PANEL_EMAIL || !PLAN_PANEL_PASSWORD,
  "E2E_PLAN_PANEL_EMAIL/E2E_ADMIN_PASSWORD not set -- only scripts/gate.sh's step_e2e sets these",
);

test("chat: the plan panel is collapsed by default, shows a live status, and expands via keyboard to the full ordered transcript", async ({
  page,
}) => {
  await loginAndReachChat(page, PLAN_PANEL_EMAIL as string, PLAN_PANEL_PASSWORD as string);
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

  // DA-14-D2-1 (ride-along, 14-D3): screenshot evidence of the COLLAPSED state, against the
  // real running app with real seeded-account data -- closes the DA's own "no screenshot
  // evidence for a user-visible UI component" low finding (text/attribute assertions above
  // cannot catch a purely visual regression). No mockup exists for this panel; this is
  // evidence of current rendering, not a mockup-diff proof.
  await panel.screenshot({ path: "test-results/plan-panel-evidence/collapsed.png" });

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

  // DA-14-D2-1: the EXPANDED state, same evidence purpose as above.
  await panel.screenshot({ path: "test-results/plan-panel-evidence/expanded.png" });

  // Collapsing again via the SAME keyboard-operable control.
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(list).toHaveCount(0);
});

test("chat: the plan panel renders identically with prefers-reduced-motion (no CSS transition to neutralise)", async ({
  page,
}) => {
  // T14.4.2's own design decision (ADR 14-D2 "Known limitations"): the panel's expand/
  // collapse is a plain conditional render, never a CSS transition, so there is nothing for
  // prefers-reduced-motion to neutralise in the first place. This test turns that CLAIM into
  // evidence: the same real flow, with reduced motion emulated from the start, screenshotted
  // expanded -- a future regression that DID add a transition/animation would still pass the
  // text/attribute assertions above but would be visible by eye in this screenshot.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await loginAndReachChat(page, PLAN_PANEL_EMAIL as string, PLAN_PANEL_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  const thread = page.getByTestId("chat-thread");
  await page.getByPlaceholder("Ask a security question…").fill(SECURITY_QUESTION);
  await page.getByRole("button", { name: "SEND" }).click();

  const panel = page.getByTestId("plan-panel");
  await expect(panel).toBeVisible({ timeout: 20_000 });
  await expect(thread.getByText(/Sources/)).toBeVisible({ timeout: 60_000 });

  const toggle = page.getByTestId("plan-panel-toggle");
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTestId("plan-panel-list")).toBeVisible();

  await panel.screenshot({ path: "test-results/plan-panel-evidence/reduced-motion-expanded.png" });
});
