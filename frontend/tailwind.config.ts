import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    // T13.J.1: lib/adminColours.ts's TONE_CLASS/TONE_TEXT_CLASS are the single source of
    // truth for status-tone utility classes (unit-tested there) -- without this, Tailwind's
    // content scan never sees those class name strings and purges them, so the mockup's
    // tinted pill backgrounds would be missing from the built CSS (found by the e2e smoke
    // test added in e2e/admin-dashboard.spec.ts: the OK pill rendered with no background at
    // all, exactly the pre-13-J bug this block fixes).
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      // Vault palette — mirrors the CSS custom properties in globals.css.
      colors: {
        vault: {
          bg: "var(--bg)",
          steel: "var(--steel)",
          "steel-dark": "var(--steel-dark)",
          "steel-light": "var(--steel-light)",
          amber: "var(--amber)",
          "amber-bright": "var(--amber-bright)",
          plate: "var(--plate)",
          danger: "var(--danger)",
          // T13.8.8 (DA-13-G2-4): lighter red for TEXT only -- see globals.css's own comment.
          "danger-text": "var(--danger-text)",
          // T13.J.1 (13-J): admin-dashboard-only status/surface tokens -- see globals.css.
          ok: "var(--ok)",
          "ok-bg": "var(--ok-bg)",
          "ok-border": "var(--ok-border)",
          warn: "var(--warn)",
          "warn-bg": "var(--warn-bg)",
          "warn-border": "var(--warn-border)",
          crit: "var(--crit)",
          "crit-bg": "var(--crit-bg)",
          "crit-border": "var(--crit-border)",
          off: "var(--off)",
          "off-bg": "var(--off-bg)",
          "off-border": "var(--off-border)",
          surface: "var(--surface)",
          "surface-raised": "var(--surface-raised)",
          line: "var(--line)",
          "text-strong": "var(--text-strong)",
          "text-muted": "var(--text-muted)",
        },
      },
      fontFamily: {
        // Monospace across the whole UI (terminal / vault look).
        sans: ["ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
        mono: ["ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
