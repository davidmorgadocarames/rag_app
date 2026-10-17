// T12.1.10: the gate's `e2e` step (--full only) starts the real backend + frontend itself
// (scripts/gate.sh's step_e2e) and passes their base URL here; `webServer` is deliberately
// NOT configured -- this file never starts anything on its own, so a stray `npx playwright
// test` run outside the gate step fails fast on a connection error instead of silently
// spinning up something unexpected.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // 90s (was 60s, T12.1.10): `auth.spec.ts`'s real-security-question test (12a-E, DEBT-035)
  // now waits out a genuinely cold Ollama generate call on top of register/login/navigation
  // -- 60s left too little margin once the chat turn alone can legitimately take several
  // seconds (see the `latency` gate step's own p95 numbers).
  timeout: 90_000,
  retries: 0,
  reporter: "list",
  use: {
    // "localhost", not "127.0.0.1": Next.js's own middleware redirects canonicalise to
    // "localhost" regardless of the actual Host header (see scripts/gate.sh's step_e2e) --
    // visiting "localhost" from the start keeps the browser's origin consistent throughout.
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:18101",
    headless: true,
    // VaultDoor's open-sequence is driven by requestAnimationFrame, which never progressed
    // in a real headless run here (reproduced: the component stayed stuck at "unlocking…"
    // until the test's own 60s timeout, confirmed by the page snapshot) -- rather than
    // masking that with a longer timeout, use the component's OWN existing accessibility
    // affordance (components/VaultDoor.tsx checks prefers-reduced-motion and skips straight
    // to the open state, synchronously, when it matches): deterministic and fast, not a
    // blanket timeout raise.
    reducedMotion: "reduce",
  },
});
