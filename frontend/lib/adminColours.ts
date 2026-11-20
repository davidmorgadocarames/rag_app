// T13.J.1 (13-J): pure, DOM-free status/surface colour data for the admin dashboard (C5
// pattern, same as adminTriage.ts) -- the Tailwind utility classes StatusBadge and every admin
// panel use for each status tone, plus the WCAG AA contrast maths that proves the mockup's
// palette (Main.dc.html, approved 2026-11-19) is legible before any component ever renders it.
//
// The raw hex/rgba values below MUST stay in sync with the CSS custom properties in
// app/globals.css (--ok/--warn/--crit/--off/--surface/--surface-raised) -- they are duplicated
// here, rather than read from the DOM, because this module is unit-tested with the plain node
// test runner (no jsdom/browser in this repo's `npm test`, see package.json).

import type { FeatureStatusKey } from "./adminTriage";

/** Tailwind utility classes for one status tone's badge/pill: tinted background + border,
 * matching text/icon colour -- never colour alone (every pill also renders an icon + label,
 * see StatusBadge.tsx). */
export const TONE_CLASS: Record<FeatureStatusKey, string> = {
  ok: "bg-vault-ok-bg border-vault-ok-border text-vault-ok",
  warn: "bg-vault-warn-bg border-vault-warn-border text-vault-warn",
  crit: "bg-vault-crit-bg border-vault-crit-border text-vault-crit",
  off: "bg-vault-off-bg border-vault-off-border text-vault-off",
};

/** Plain (non-tinted) text colour for a tone, for places that colour TEXT without a pill
 * (e.g. the triage status filter chips, DEC-styled countdown text) -- same hue as TONE_CLASS. */
export const TONE_TEXT_CLASS: Record<FeatureStatusKey, string> = {
  ok: "text-vault-ok",
  warn: "text-vault-warn",
  crit: "text-vault-crit",
  off: "text-vault-off",
};

// ---------------------------------------------------------------------------------------------
// WCAG AA contrast maths (relative luminance -> contrast ratio), standard formulas from
// https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio -- duplicated nowhere else in this repo.
// ---------------------------------------------------------------------------------------------

export type Rgb = readonly [number, number, number];

export function hexToRgb(hex: string): Rgb {
  const clean = hex.replace("#", "");
  const n = (i: number) => parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  return [n(0), n(1), n(2)];
}

function srgbToLinear(channel: number): number {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

export function relativeLuminance([r, g, b]: Rgb): number {
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

/** WCAG contrast ratio between two colours (order-independent), each a `#rrggbb` hex string. */
export function contrastRatio(hexA: string, hexB: string): number {
  const lumA = relativeLuminance(hexToRgb(hexA));
  const lumB = relativeLuminance(hexToRgb(hexB));
  const lighter = Math.max(lumA, lumB);
  const darker = Math.min(lumA, lumB);
  return (lighter + 0.05) / (darker + 0.05);
}

/** Alpha-composites `tintHex` at `alpha` over opaque `baseHex` -- used to compute the EFFECTIVE
 * background of a tinted pill (rgba bg) sitting on a card/page surface, so contrast can be
 * checked against what is actually painted, not the tint colour alone. */
export function compositeOver(tintHex: string, alpha: number, baseHex: string): Rgb {
  const [tr, tg, tb] = hexToRgb(tintHex);
  const [br, bg, bb] = hexToRgb(baseHex);
  const mix = (t: number, b: number) => Math.round(t * alpha + b * (1 - alpha));
  return [mix(tr, br), mix(tg, bg), mix(tb, bb)];
}

export function rgbToHex([r, g, b]: Rgb): string {
  const h = (c: number) => c.toString(16).padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}

export const WCAG_AA_NORMAL_TEXT = 4.5;

// Page/surface backgrounds a status pill can actually sit on in the admin dashboard (mirrors
// --bg/--surface/--surface-raised in globals.css).
export const PAGE_BG = "#0a0a0c";
export const SURFACE_BG = "#131418";
export const SURFACE_RAISED_BG = "#16181d";

/** Each tone's text colour + its own tinted-pill background (opaque tint colour + alpha),
 * mirroring --ok/--warn/--crit/--off(-bg) in globals.css exactly. */
export const TONE_PALETTE: Record<FeatureStatusKey, { text: string; tintHex: string; alpha: number }> = {
  ok: { text: "#8fd6b0", tintHex: "#6cc597", alpha: 0.12 },
  warn: { text: "#f0b968", tintHex: "#f0b968", alpha: 0.12 },
  crit: { text: "#f29a85", tintHex: "#ef8a73", alpha: 0.14 },
  off: { text: "#b4bcc6", tintHex: "#9aa3ae", alpha: 0.1 },
};

/** The two surfaces a tinted pill is actually painted on in this dashboard (a bare card, or the
 * raised feature drawer) -- contrast must hold against both. */
export const PILL_BACKDROPS: readonly string[] = [SURFACE_BG, SURFACE_RAISED_BG];
