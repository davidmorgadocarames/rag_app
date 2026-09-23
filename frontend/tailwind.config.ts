import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
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
