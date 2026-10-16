// T12.1.10: the gate's `e2e` step (--full only) starts the real backend + frontend itself
// (scripts/gate.sh's step_e2e) and passes their base URL here; `webServer` is deliberately
// NOT configured -- this file never starts anything on its own, so a stray `npx playwright
// test` run outside the gate step fails fast on a connection error instead of silently
// spinning up something unexpected.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:18101",
    headless: true,
  },
});
