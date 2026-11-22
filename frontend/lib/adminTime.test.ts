import assert from "node:assert/strict";
import { test } from "node:test";

import { formatCompletedAt } from "./adminTime.ts";

// DA-13-J3J4-3: the mockup's own exact sample string, "Completed 17 Nov 22:10" -- this proves
// the HELPER's shape; ErasurePanel.tsx prefixes "Completed " itself.
test("formatCompletedAt matches the mockup's own sample instant exactly", () => {
  assert.equal(formatCompletedAt("2026-11-17T22:10:00Z"), "17 Nov 22:10");
});

test("formatCompletedAt is always UTC, never the host's local timezone", () => {
  // A time that would shift to a different DAY in most non-UTC zones if this ever read
  // local time instead of UTC fields.
  assert.equal(formatCompletedAt("2026-01-01T00:05:00Z"), "1 Jan 00:05");
});

test("formatCompletedAt pads a single-digit hour and minute", () => {
  assert.equal(formatCompletedAt("2026-03-05T04:07:00Z"), "5 Mar 04:07");
});

test("formatCompletedAt drops seconds, milliseconds and the trailing Z", () => {
  const formatted = formatCompletedAt("2026-06-30T13:45:59.999Z");
  assert.equal(formatted, "30 Jun 13:45");
  assert.ok(!formatted.includes(":45:59"));
  assert.ok(!formatted.includes("Z"));
});

test("formatCompletedAt never shows a year", () => {
  assert.ok(!formatCompletedAt("2026-11-17T22:10:00Z").includes("2026"));
});

test("formatCompletedAt handles a cross-year, cross-midnight instant", () => {
  assert.equal(formatCompletedAt("2026-12-31T23:59:00Z"), "31 Dec 23:59");
  assert.equal(formatCompletedAt("2027-01-01T00:00:00Z"), "1 Jan 00:00");
});

test("formatCompletedAt falls back to the raw input for an unparsable value, never throws", () => {
  assert.equal(formatCompletedAt("not-a-date"), "not-a-date");
});
