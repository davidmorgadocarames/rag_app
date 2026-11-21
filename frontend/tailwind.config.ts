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
          // T13.12.1 (13-J3): named, pre-composited amber washes -- see globals.css's own
          // comment for why `bg-vault-amber/10` (an opacity modifier on a var() colour)
          // compiles to no CSS rule at all, and why these exist as plain, un-modified colours
          // instead (a plain `bg-vault-amber-wash-10` class needs no opacity math).
          "amber-wash-10": "var(--amber-wash-10)",
          "amber-wash-12": "var(--amber-wash-12)",
          "amber-wash-14": "var(--amber-wash-14)",
          // T13.12.1 (13-J3): same fix, for `border-vault-line/60`/`/70` (also an opacity
          // modifier on a var() colour, also compiled to no rule) -- see globals.css.
          "row-divider": "var(--row-divider)",
          "stage-divider": "var(--stage-divider)",
        },
      },
      fontFamily: {
        // Monospace across the whole UI (terminal / vault look).
        sans: ["ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
        mono: ["ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
      // DA-13-J-1 fix (13-J2): the mockup's own card/panel radius (Main.dc.html lines
      // 59/70/86/120/166/215/260/305 -- 10px cards, 8px inner probe card, 6px buttons),
      // admin dashboard scope only. Named so no component ever hard-codes a hex/px radius
      // directly -- Tailwind's own `rounded-lg`/`rounded` defaults (8px/4px) never matched.
      borderRadius: {
        "admin-card": "10px",
        "admin-card-inner": "8px",
        "admin-btn": "6px",
        // T13.12.1 (13-J3 correction): the mockup's left-hand section nav button is its OWN
        // 8px radius (Main.dc.html `sections[].style`: `border-radius: 8px`), a DIFFERENT
        // value from every other admin button's 6px (`admin-btn`) -- 13-J2's DA-13-J-1 fix
        // used `admin-btn` (6px) for the nav item too, which this block corrects (the e2e
        // radius assertion in e2e/admin-dashboard.spec.ts is updated to match).
        "admin-nav": "8px",
      },
    },
  },
  plugins: [],
};

export default config;
