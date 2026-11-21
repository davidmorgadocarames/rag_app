// T13.12.1 (13-J3, DEBT-059 reopened): Tailwind 3 silently emits NO CSS rule for an opacity
// modifier (`/<n>`) applied to a colour whose Tailwind value is a CSS custom property
// (`vault.amber: var(--amber)`, tailwind.config.ts) -- there is no literal rgb triplet for it
// to scale. `bg-vault-amber/10` (the selected triage row), the active nav item's
// `bg-vault-amber/10` (should have been a DIFFERENT 0.12 tint) and the pressed filter chip's
// `bg-vault-amber/10` (should have been 0.14) all compiled to nothing -- found live by the
// user ("mucho menos colorido" than the mockup) after 13-J/13-J2 had already shipped and
// `lib/adminColours.test.ts` had already proven the STATIC tones (ok/warn/crit/off, which
// never use an opacity modifier) pass WCAG AA. This file closes that gap at the Tailwind
// compile layer itself, not just by eyeballing the fix:
//   1. a static grep that fails the moment ANY admin component writes an opacity modifier on
//      a `vault-*` colour class again (the rule this block's ADR note makes permanent), and
//   2. an actual Tailwind compile of every admin component's source, asserting every `vault-*`
//      utility class those components use produces a real CSS rule -- with a negative control
//      that proves this compile-check method would truly have caught the original bug.

import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import postcss from "postcss";
import tailwindcss from "tailwindcss";

import baseConfig from "../tailwind.config.ts";

const ADMIN_DIR = path.join(import.meta.dirname, "..", "components", "admin");

function adminSourceFiles(): string[] {
  return readdirSync(ADMIN_DIR)
    .filter((f) => f.endsWith(".tsx"))
    .map((f) => path.join(ADMIN_DIR, f));
}

// T13.12.1: the shape of the original bug -- any `vault-*` colour class with a trailing
// `/<digits>` opacity modifier, wherever it appears in a className string (plain or inside a
// template literal/ternary).
const OPACITY_MODIFIER_RE = /[a-z0-9-]*vault-[a-z0-9-]+\/\d+/g;

test("no admin component applies an opacity modifier to a vault-* colour class", () => {
  const offenders: string[] = [];
  for (const file of adminSourceFiles()) {
    const text = readFileSync(file, "utf8");
    const matches = text.match(OPACITY_MODIFIER_RE);
    if (matches) offenders.push(...matches.map((m) => `${path.basename(file)}: ${m}`));
  }
  assert.deepEqual(
    offenders,
    [],
    `opacity modifier on a vault-* colour (compiles to no CSS rule, T13.12.1): ${offenders.join(", ")}`,
  );
});

// Plain, unmodified `vault-*` utility classes (no `/` opacity modifier, no arbitrary-value
// brackets), with an optional single variant prefix (only "hover:" is actually used alongside
// a vault-* colour in this codebase today, e.g. `hover:border-vault-steel`) -- the shape every
// admin component uses for its tinted surfaces. Kept deliberately simple (one variant level,
// no CSS escaping beyond the colon) rather than trying to generically match every possible
// Tailwind class shape.
const CLASS_TOKEN_RE = /\b(?:[a-z-]+:)?(?:bg|border|text|shadow)-vault-[a-z0-9-]+\b/g;

function usedClassTokens(files: string[]): Set<string> {
  const tokens = new Set<string>();
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    for (const m of text.matchAll(CLASS_TOKEN_RE)) tokens.add(m[0]);
  }
  return tokens;
}

async function compileWithContent(content: string[]): Promise<string> {
  const config = { ...baseConfig, content };
  const result = await postcss([tailwindcss(config)]).process("@tailwind utilities;", {
    from: undefined,
  });
  return result.css;
}

// Tailwind escapes a variant's ":" as "\:" in the class selector itself, then appends the
// variant's own pseudo-selector after it (e.g. `hover:border-vault-steel` ->
// `.hover\:border-vault-steel:hover{...}`) -- this only needs to handle "hover:" (the one
// variant actually used with a vault-* colour here, confirmed by grep above).
const VARIANT_PSEUDO: Record<string, string> = { hover: ":hover" };

function hasRuleFor(css: string, token: string): boolean {
  const colonIndex = token.indexOf(":");
  const variant = colonIndex === -1 ? null : token.slice(0, colonIndex);
  const selectorClass =
    variant === null ? token : `${variant}\\:${token.slice(colonIndex + 1)}`;
  const pseudoSuffix = variant !== null ? (VARIANT_PSEUDO[variant] ?? "") : "";
  const selector = `.${selectorClass}${pseudoSuffix}`;
  return css.includes(`${selector}{`) || css.includes(`${selector} {`);
}

test("every plain vault-* utility class used in an admin component compiles to a real CSS rule", async () => {
  const files = adminSourceFiles();
  const css = await compileWithContent(files);
  const tokens = [...usedClassTokens(files)];
  assert.ok(
    tokens.length > 10,
    "sanity check: expected to find plenty of vault-* classes across components/admin",
  );
  const missing = tokens.filter((t) => !hasRuleFor(css, t));
  assert.deepEqual(
    missing,
    [],
    `these admin classes compiled to no CSS rule at all: ${missing.join(", ")}`,
  );
});

// Negative control (T13.12.1): proves the compile-check above would really have caught the
// original bug -- `bg-vault-amber/10` on `vault.amber: var(--amber)` compiles to nothing.
test("negative control: bg-vault-amber/10 (the original bug) compiles to no CSS rule", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "admin-tw-negctrl-"));
  const fixture = path.join(dir, "fixture.tsx");
  writeFileSync(fixture, '<div className="bg-vault-amber/10" />', "utf8");
  const css = await compileWithContent([fixture]);
  assert.ok(
    !css.includes("bg-vault-amber") || !/\.bg-vault-amber\\\/10\s*\{/.test(css),
    "expected bg-vault-amber/10 to compile to no usable rule -- this is the exact bug T13.12.1 fixed",
  );
});
