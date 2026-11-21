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

// T13.8.4-T13.8.6/13-G2: the redesigned shell -- header, KPI tiles, the Triage section (the
// default one) with its status filters and feature drawer, plus every other section's
// existing panel still reachable through the new left-hand menu.
test("an admin can enrol MFA, step up, and see the redesigned dashboard shell", async ({
  page,
}) => {
  await loginAndReachChat(page, ADMIN_EMAIL as string, ADMIN_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);

  adminSecret = await enrolAndStepUpMfa(page);

  await page.goto("/admin");
  await expect(page.getByTestId("admin-header-title")).toBeVisible();
  await expect(page.getByText("Operations dashboard")).toBeVisible();

  // T13.5.2: a freshly seeded e2e stack has nothing to alert on -- no banner at all.
  await expect(page.getByTestId("admin-alert-banner")).toHaveCount(0);

  await expect(page.getByTestId("admin-kpi-tiles")).toBeVisible();

  // Triage is the default section: every registry feature, including admin-api itself.
  await expect(page.getByTestId("admin-triage-panel")).toBeVisible();
  await expect(page.getByTestId("triage-row-admin-api")).toBeVisible();

  // DA-13-J-1 fix (13-J2): the mockup's own card radius (10px) -- `rounded-admin-card`
  // (tailwind.config.ts). Fails on the pre-fix code (Tailwind's own `rounded-lg` default: 8px).
  await expect(page.getByTestId("admin-triage-panel")).toHaveCSS("border-radius", "10px");
  // T13.12.1 correction (13-J3): the nav item's OWN radius is the mockup's 8px
  // (`rounded-admin-nav`, Main.dc.html `sections[].style`), a DIFFERENT value from the
  // generic 6px `rounded-admin-btn` 13-J2 mistakenly reused here (that assertion used to read
  // "6px" -- updated now that the root cause is understood).
  await expect(page.getByTestId("admin-nav-pipeline")).toHaveCSS("border-radius", "8px");

  // T13.12.1/T13.12.3 (13-J3): the active nav item (Triage, the default section) and the
  // pressed filter chip ("All", the default filter) now actually paint their amber tint --
  // fails on the pre-13-J3 code, where `bg-vault-amber/10` (an opacity modifier on a var()
  // colour) compiled to no CSS rule at all, so both had NO background whatsoever.
  await expect(page.getByTestId("admin-nav-triage")).toHaveCSS(
    "background-color",
    "rgba(217, 154, 63, 0.12)",
  );
  await expect(page.getByTestId("triage-filter-all")).toHaveCSS(
    "background-color",
    "rgba(217, 154, 63, 0.14)",
  );
  // Fix (user 2026-11-22, from the screenshot review): the mockup's own
  // `{{f.label}} <span>{{f.count}}</span>` renders a real space ("All 38"); the pre-fix JSX
  // had a bare space between two text nodes inside a `flex` button, which flex layout
  // collapses -- rendered as "All38"/"Critical1" glued together. Asserts the real visible
  // text has the space, against whatever the live registry count actually is.
  await expect(page.getByTestId("triage-filter-all")).toHaveText(/^All \d+$/);

  // A filter narrows the list (T13.8.5): a freshly seeded stack has no Critical feature.
  await page.getByTestId("triage-filter-crit").click();
  await expect(page.getByTestId("triage-row-admin-api")).toHaveCount(0);
  await expect(page.getByText("Nothing in this state right now.")).toBeVisible();
  await page.getByTestId("triage-filter-all").click();

  // T13.J.1: the OK tone now paints the mockup's own tinted pill (dedicated green token) --
  // a freshly seeded e2e stack guarantees at least one OK row (admin-api itself), so this is
  // the one tone always exercisable live end-to-end without touching backend probe data
  // (warn/crit/off have no live-data path here without a behaviour change out of this
  // block's scope -- they are covered at the unit level instead, lib/adminColours.test.ts).
  // This fails on the pre-13-J code: the old OK badge had no background at all (border
  // only, reusing the neutral steel-light token).
  const adminApiBadge = page
    .getByTestId("triage-row-admin-api")
    .getByText("OK", { exact: true });
  await expect(adminApiBadge).toBeVisible();
  await expect(adminApiBadge).toHaveCSS("background-color", "rgba(108, 197, 151, 0.12)");
  await expect(adminApiBadge).toHaveCSS("color", "rgb(143, 214, 176)");

  // Selecting a row opens the drawer with that feature (T13.8.6).
  await page.getByTestId("triage-row-admin-api").click();
  await expect(page.getByTestId("admin-feature-drawer")).toBeVisible();
  await expect(page.getByTestId("admin-feature-drawer-id")).toHaveText("admin-api");
  // User decision (2026-11-22, overrides the mockup): the drawer's "Docs: …" line is removed
  // entirely ("no sirve para nada") -- proves it stays gone.
  await expect(page.getByTestId("admin-feature-drawer").getByText(/^Docs:/)).toHaveCount(0);
  // T13.12.1/T13.12.3: the selected row's own amber tint -- fails on the pre-13-J3 code (same
  // `bg-vault-amber/10` opacity-modifier bug, no background at all).
  await expect(page.getByTestId("triage-row-admin-api")).toHaveCSS(
    "background-color",
    "rgba(217, 154, 63, 0.1)",
  );
  // T13.12.2: the drawer's own radius (10px) -- a DIFFERENT, matching value to the triage
  // panel's (not the pre-13-J3 `rounded-lg` default of 8px).
  await expect(page.getByTestId("admin-feature-drawer")).toHaveCSS("border-radius", "10px");
  // T13.8.8/DA-13-G2-3: the drawer also shows the registry's own lifecycle status (a
  // DIFFERENT field from the probe status badge above it) -- admin-api is "enabled".
  await expect(page.getByTestId("admin-feature-drawer-lifecycle-status")).toContainText(
    "enabled",
  );

  // T13.8.7: the redesigned Pipeline section (waterfall from the `pipeline` field, 13-G3).
  // Whether THIS backend process has already answered a real chat turn by the time this test
  // runs depends on test-file scheduling (shared in-process histograms, T13.5.2-style
  // caveat) -- assert the one thing that is always true either way: exactly one of the empty
  // state or the "Expand all" control renders, never neither.
  await page.getByTestId("admin-nav-pipeline").click();
  await expect(page.getByTestId("admin-latency-panel")).toBeVisible();
  await expect(
    page.getByTestId("pipeline-empty-state").or(page.getByTestId("pipeline-expand-all")),
  ).toBeVisible();

  // T13.8.8: the redesigned Quality section -- same reasoning: whichever state it is in
  // (nothing run yet on this checkout vs. a real `eval` gate run earlier in this `--full`),
  // exactly one of the empty state or the metrics table renders.
  await page.getByTestId("admin-nav-quality").click();
  await expect(page.getByTestId("admin-quality-panel")).toBeVisible();
  await expect(
    page
      .getByTestId("quality-empty-state")
      .or(page.getByTestId("quality-metric-retrieval_recall")),
  ).toBeVisible();

  await page.getByTestId("admin-nav-erasure").click();
  await expect(page.getByTestId("admin-erasure-panel")).toBeVisible();
});

test("the admin can erase another account's data through the erasure panel", async ({ page }) => {
  await loginAndReachChat(page, ADMIN2_EMAIL as string, ADMIN_PASSWORD as string);
  await expect(page).toHaveURL(/\/chat$/);
  await enrolAndStepUpMfa(page);

  await page.goto("/admin");
  await page.getByTestId("admin-nav-erasure").click();
  const form = page.getByTestId("erase-account-form");
  await expect(form).toBeVisible();
  await form.getByLabel("Account email").fill(ERASE_TARGET_EMAIL as string);
  await form.getByLabel("Type ERASE to confirm").fill("ERASE");
  await form.getByRole("button", { name: "Erase account" }).click();

  // The response never echoes the submitted email -- only an opaque request id + detail.
  await expect(form.getByText(/request /)).toBeVisible();
  await expect(page.getByText(ERASE_TARGET_EMAIL as string)).toHaveCount(0);

  // The erasure panel's table now lists at least one open request, with its status shown as
  // a real icon+text StatusBadge (T13.8.8), not plain text.
  const firstRow = page.getByTestId("admin-erasure-panel").locator("tbody tr").first();
  await expect(firstRow).toBeVisible();
  await expect(firstRow.getByText(/Pending|Running/)).toBeVisible();
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

// T13.12.3 (13-J3): visual evidence against a SEEDED state (>= 1 critical, 1 warning, 1 off,
// 2 alerts, 1 erasure request near its deadline) -- a freshly seeded e2e stack never produces
// any of these for real (no probe is actually down, no erasure request is actually 20h+ old),
// so this test mocks the backend's OWN wire shapes instead of the real `/admin/*` data:
//   - `/api/admin/stream` (SSE, local `ADMIN_LIVE_MODE=sse`, scripts/gate.sh): fulfilled with
//     one real `event: snapshot` frame (the exact `rag_app.api.admin._sse_frame` shape,
//     `lib/adminStream.ts`) carrying the seeded features/erasure/alerts.
//   - `/api/admin/erasure`/`/api/admin/latency`/`/api/admin/eval`: the dashboard's OWN first-
//     load effect (AdminDashboard.tsx) fetches these three separately on mount regardless of
//     live mode, so they are mocked too (with the SAME erasure payload as the SSE snapshot).
// Reuses ADMIN_EMAIL's account (already enrolled by the first test in this file; tests run
// sequentially, no `fullyParallel`) via `stepUpOnlyMfa` in a brand-new browser context --
// never re-enrols, never adds to the real backend's probe/erasure state.
test("the seeded dashboard matches the mockup's palette across every section (visual evidence)", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await loginAndReachChat(page, ADMIN_EMAIL as string, ADMIN_PASSWORD as string);
    await expect(page).toHaveURL(/\/chat$/);
    await stepUpOnlyMfa(page, adminSecret as string);

    const seededFeatures = [
      {
        id: "admin-api",
        name: "Admin API",
        phase: "13",
        status: "enabled",
        probe_status: "ok",
        probe_detail: "probe ok",
        metrics: {},
        dev_only: false,
        azure_only: false,
        expected_here: true,
        docs: "docs/TRD.md#admin-api",
      },
      {
        id: "identity-email-verification-reset",
        name: "E-mail verification + reset",
        phase: "12",
        status: "enabled",
        probe_status: "down",
        probe_detail: "down: SMTP send failed 3 times",
        metrics: { 'secrag_email_send_total{result="error"}': 3 },
        dev_only: false,
        azure_only: false,
        expected_here: true,
        docs: "docs/RUNBOOK_AZURE.md#email",
      },
      {
        id: "rag-rerank",
        name: "Rerank stage",
        phase: "9",
        status: "enabled",
        probe_status: "degraded",
        probe_detail: "degraded: p95 2.1s, above the 2s target",
        metrics: {},
        dev_only: false,
        azure_only: false,
        expected_here: true,
        docs: "docs/adr/adr_phase9_rerank.md",
      },
      {
        // T13.12 fix (13-J3, user 2026-11-22): "ok" here (not "off") so the Defence lab tab
        // itself is available in this scenario -- the user asked for a defence-lab screenshot
        // too, which needs the panel actually reachable. "azure-internal-ingress" below is the
        // scenario's "off" example instead (same tone the mockup itself uses "admin-defence-
        // lab" for -- fixture-only: the real backend's azure-internal-ingress entry has no
        // health probe at all and would never be "down" for real, see features.py).
        id: "admin-defence-lab",
        name: "Defence lab",
        phase: "13",
        status: "enabled",
        probe_status: "ok",
        probe_detail: "probe ok",
        metrics: {},
        dev_only: true,
        azure_only: false,
        expected_here: true,
        docs: "docs/security/DEFENCE_LAB.md",
      },
      {
        id: "azure-internal-ingress",
        name: "Internal ingress (backend has no public URL)",
        phase: "12b",
        status: "enabled",
        probe_status: "down",
        probe_detail: "off by design on this stack",
        metrics: {},
        dev_only: false,
        azure_only: true,
        expected_here: false,
        docs: "docs/TRD.md",
      },
    ];
    const seededAlerts = [
      {
        code: "email_failures",
        severity: "amber",
        message: "E-mail send failures: 3 since this replica started.",
        feature_ids: ["identity-email-verification-reset"],
      },
      {
        code: "erasure_approaching",
        severity: "amber",
        message: "1 erasure request has less than 6h left before its 24h deadline.",
        feature_ids: ["privacy-asynchronous-erasure"],
      },
    ];
    const seededErasure = {
      requests: [
        {
          id: "er_7f3a91c2",
          requested_at: "2026-11-17T06:00:00Z",
          age_seconds: 68400,
          seconds_left: 10000, // < AMBER_THRESHOLD_SECONDS (4h) -- "near deadline"
          status: "running",
          rows_remaining: { users: 214 },
          attempts: 2,
          last_error_class: "SMTPError",
          overdue: false,
        },
        {
          id: "er_1b08d4e7",
          requested_at: "2026-11-17T17:00:00Z",
          age_seconds: 21600,
          seconds_left: 64800,
          status: "pending",
          rows_remaining: { users: 1032 },
          attempts: 0,
          last_error_class: null,
          overdue: false,
        },
        {
          id: "er_c44e0f19",
          requested_at: "2026-11-16T22:00:00Z",
          age_seconds: 3600,
          seconds_left: 0,
          status: "done",
          rows_remaining: {},
          attempts: 1,
          last_error_class: null,
          overdue: false,
          completed_at: "2026-11-17T22:10:00Z",
        },
      ],
      last_purger_run: null,
    };
    const seededLatency = {
      stages: [{ stage: "total", count: 128, avg_seconds: 4.62, p50_seconds: 4.1, p95_seconds: 6.0 }],
      pipeline: [
        {
          stage: "classifying",
          steps: [
            {
              stage: "classifying",
              step: "classify_intent",
              count: 128,
              avg_seconds: 0.06,
              p50_seconds: 0.05,
              p95_seconds: 0.09,
            },
          ],
        },
        {
          stage: "generating",
          steps: [
            {
              stage: "generating",
              step: "token_stream",
              count: 120,
              avg_seconds: 1.9,
              p50_seconds: 1.7,
              p95_seconds: 2.95,
            },
          ],
        },
      ],
    };
    const seededEval = {
      last_run_at: "2026-11-17T09:46:00Z",
      passed: true,
      metrics: [
        { key: "hit_rate@5", value: 0.92, floor: 0.85, baseline: 0.9, below_floor: false, regressed: false },
        {
          key: "abstention_accuracy",
          value: 0.88,
          floor: 0.8,
          baseline: 0.91,
          below_floor: false,
          regressed: true,
        },
      ],
    };

    await page.route("**/api/admin/stream", async (route) => {
      const payload = { features: seededFeatures, erasure: seededErasure, alerts: seededAlerts };
      // Same frame shape as `rag_app.api.admin._sse_frame` (`event: <type>\ndata: <json>\n\n`)
      // -- `retry: 60000` so a reconnect (the fulfilled body ends the connection) never
      // re-fires within this test's own lifetime.
      const body = `retry: 60000\n\nevent: snapshot\ndata: ${JSON.stringify(payload)}\n\n`;
      await route.fulfill({ status: 200, contentType: "text/event-stream", body });
    });
    await page.route("**/api/admin/erasure", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(seededErasure) }),
    );
    await page.route("**/api/admin/latency", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(seededLatency) }),
    );
    await page.route("**/api/admin/eval", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(seededEval) }),
    );
    const seededLabRun = {
      detail: "ok",
      git_sha: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
      all_matched: false,
      results: [
        {
          probe_id: "login-brute-force",
          expected_outcome: "locked after repeated bad passwords",
          observed_outcome: "held · 2 min ago",
          passed: true,
        },
        {
          probe_id: "burst-requests",
          expected_outcome: "429 after burst",
          observed_outcome: "held · 2 min ago",
          passed: true,
        },
        {
          probe_id: "session-replay-after-logout",
          expected_outcome: "revoked session rejected",
          observed_outcome: "not run yet",
          passed: false,
        },
      ],
    };
    await page.route("**/api/admin/lab/run", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(seededLabRun) }),
    );

    await page.goto("/admin");
    await expect(page.getByTestId("admin-triage-panel")).toBeVisible();
    // The SSE snapshot replaced the SSR shell's real (clean) data -- both alerts now show.
    await expect(page.getByTestId("admin-alert-banner")).toBeVisible();
    await expect(page.getByTestId("admin-alert-email_failures")).toBeVisible();
    await expect(page.getByTestId("admin-alert-erasure_approaching")).toBeVisible();

    // At least one of each tone is present (T13.12.3's seed requirement).
    await expect(page.getByTestId("triage-row-identity-email-verification-reset")).toContainText(
      "Critical",
    );
    await expect(page.getByTestId("triage-row-rag-rerank")).toContainText("Warning");
    await expect(page.getByTestId("triage-row-azure-internal-ingress")).toContainText("Off here");
    await expect(page.getByTestId("triage-row-admin-api")).toContainText("OK");

    // T13.12.2: the "Erasure queue" KPI sub-line now shows the near-deadline request, tinted
    // amber (0 overdue, 1 near deadline -- fails on the pre-13-J3 text, which dropped the
    // near-deadline clause entirely and so never coloured the sub-line for this case).
    const erasureKpiSub = page.getByTestId("admin-kpi-tiles").getByText(/near deadline/);
    await expect(erasureKpiSub).toBeVisible();
    await expect(erasureKpiSub).toHaveCSS("color", "rgb(240, 185, 104)");

    await page.screenshot({ path: "test-results/admin-screens/01-triage.png", fullPage: true });

    await page.getByTestId("triage-row-identity-email-verification-reset").click();
    await expect(page.getByTestId("admin-feature-drawer-why")).toHaveCount(0); // real failure, no "why"
    await page.screenshot({ path: "test-results/admin-screens/02-triage-drawer.png", fullPage: true });

    await page.getByTestId("admin-nav-pipeline").click();
    await expect(page.getByTestId("admin-latency-panel")).toBeVisible();
    await page.screenshot({ path: "test-results/admin-screens/03-pipeline.png", fullPage: true });

    await page.getByTestId("admin-nav-quality").click();
    await expect(page.getByTestId("admin-quality-panel")).toBeVisible();
    await page.screenshot({ path: "test-results/admin-screens/04-quality.png", fullPage: true });

    await page.getByTestId("admin-nav-erasure").click();
    await expect(page.getByTestId("admin-erasure-panel")).toBeVisible();
    await page.screenshot({ path: "test-results/admin-screens/05-erasure.png", fullPage: true });

    // Defence lab (T13.3.1): available here (probe "ok") so it can be screenshotted too, per
    // the user's 2026-11-22 request -- "Run defence lab" uses the mocked `/admin/lab/run`
    // above, giving a populated card grid (2 held, 1 mismatch) rather than the empty state.
    await page.getByTestId("admin-nav-lab").click();
    await expect(page.getByTestId("admin-defence-lab-panel")).toBeVisible();
    await page.getByTestId("defence-lab-run-button").click();
    await expect(page.getByTestId("defence-lab-cards")).toBeVisible();
    await expect(page.getByTestId("defence-lab-card-session-replay-after-logout")).toContainText(
      "Mismatch",
    );
    await page.screenshot({ path: "test-results/admin-screens/06-defence-lab.png", fullPage: true });
  } finally {
    await context.close();
  }
});
