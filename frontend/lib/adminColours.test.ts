import assert from "node:assert/strict";
import { test } from "node:test";

import type { FeatureStatusKey } from "./adminTriage.ts";
import {
  compositeOver,
  contrastRatio,
  PAGE_BG,
  PILL_BACKDROPS,
  relativeLuminance,
  rgbToHex,
  TONE_CLASS,
  TONE_PALETTE,
  TONE_TEXT_CLASS,
  WCAG_AA_NORMAL_TEXT,
} from "./adminColours.ts";

const TONES: FeatureStatusKey[] = ["ok", "warn", "crit", "off"];

test("every tone has a distinct, non-empty badge class (icon + tinted pill, never bare colour)", () => {
  const seen = new Set<string>();
  for (const tone of TONES) {
    const cls = TONE_CLASS[tone];
    assert.ok(cls.includes("bg-vault-"), `${tone} badge class is missing a tinted background`);
    assert.ok(cls.includes("border-vault-"), `${tone} badge class is missing a border`);
    assert.ok(cls.includes("text-vault-"), `${tone} badge class is missing a text colour`);
    assert.ok(!seen.has(cls), `${tone} badge class duplicates another tone's class`);
    seen.add(cls);
  }
});

test("StatusBadge's OK tone is no longer the neutral steel-light fallback (DA-13-G2-4 gap)", () => {
  assert.ok(!TONE_CLASS.ok.includes("steel-light"), "OK must use a dedicated success token");
  assert.ok(TONE_CLASS.ok.includes("vault-ok"), "OK must use the new --ok token");
});

test("warn/crit/off each use their own dedicated token, not a reused neutral/danger one", () => {
  assert.ok(TONE_CLASS.warn.includes("vault-warn"));
  assert.ok(TONE_CLASS.crit.includes("vault-crit"));
  assert.ok(TONE_CLASS.off.includes("vault-off"));
  // Previously crit silently reused --danger/--danger-text (a different hue from the mockup).
  assert.ok(!TONE_CLASS.crit.includes("danger"));
});

test("TONE_TEXT_CLASS mirrors TONE_CLASS's text colour for plain (non-pill) text uses", () => {
  for (const tone of TONES) {
    assert.ok(TONE_CLASS[tone].includes(TONE_TEXT_CLASS[tone]));
  }
});

// --- WCAG AA contrast maths itself -----------------------------------------------------------

test("contrastRatio is symmetric and 1:1 for identical colours", () => {
  assert.equal(contrastRatio("#000000", "#000000"), 1);
  assert.equal(contrastRatio("#ffffff", "#000000"), contrastRatio("#000000", "#ffffff"));
});

test("contrastRatio matches the known WCAG reference pair (black on white = 21:1)", () => {
  assert.ok(Math.abs(contrastRatio("#000000", "#ffffff") - 21) < 0.01);
});

test("relativeLuminance of white is 1 and of black is 0", () => {
  assert.equal(relativeLuminance([255, 255, 255]), 1);
  assert.equal(relativeLuminance([0, 0, 0]), 0);
});

test("compositeOver at alpha=0 returns the base colour untouched", () => {
  assert.equal(rgbToHex(compositeOver("#ffffff", 0, "#123456")), "#123456");
});

test("compositeOver at alpha=1 returns the tint colour untouched", () => {
  assert.equal(rgbToHex(compositeOver("#123456", 1, "#ffffff")), "#123456");
});

// --- The actual mockup palette: every tone's TEXT colour on its own tinted-pill BACKGROUND,
// composited over every surface a pill can realistically sit on (card + raised drawer) --------

test("every status tone clears WCAG AA (>= 4.5:1) for text on its own tinted pill background", () => {
  const failures: string[] = [];
  for (const tone of TONES) {
    const { text, tintHex, alpha } = TONE_PALETTE[tone];
    for (const backdrop of PILL_BACKDROPS) {
      const effectiveBg = rgbToHex(compositeOver(tintHex, alpha, backdrop));
      const ratio = contrastRatio(text, effectiveBg);
      if (ratio < WCAG_AA_NORMAL_TEXT) {
        failures.push(`${tone} on ${backdrop}: ${ratio.toFixed(2)}:1 (effective bg ${effectiveBg})`);
      }
    }
  }
  assert.deepEqual(failures, [], `tones failing WCAG AA:\n${failures.join("\n")}`);
});

test("the alert banner's tinted box background (text directly on the tint, no extra pill) also clears AA", () => {
  // AlertBanner/mockup boxStyle paints the tint directly as the BOX background over the page
  // (never a second pill inside it) -- the banner's own message text is --text-strong, checked
  // here against the worst-case (most saturated/darkest) tone box, warn/crit.
  const strongText = "#e4e7eb";
  for (const tone of ["warn", "crit"] as const) {
    const { tintHex, alpha } = TONE_PALETTE[tone];
    const effectiveBg = rgbToHex(compositeOver(tintHex, alpha, PAGE_BG));
    assert.ok(
      contrastRatio(strongText, effectiveBg) >= WCAG_AA_NORMAL_TEXT,
      `${tone} alert box background fails AA for strong text`,
    );
  }
});
