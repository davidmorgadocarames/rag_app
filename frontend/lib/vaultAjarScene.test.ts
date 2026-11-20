import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AJAR_COLS,
  AJAR_ROWS,
  STICKMAN_FRAMES,
  STICKMAN_FRAME_SEQUENCE,
  buildAjarGrid,
  gridToHtml,
  nextFrameIndex,
  shouldAnimate,
} from "./vaultAjarScene.ts";

test("the grid has the expected fixed dimensions", () => {
  const grid = buildAjarGrid(0);
  assert.equal(grid.length, AJAR_ROWS);
  for (const row of grid) assert.equal(row.length, AJAR_COLS);
});

test("prefers-reduced-motion means no animation at all; otherwise it animates", () => {
  assert.equal(shouldAnimate(true), false);
  assert.equal(shouldAnimate(false), true);
});

test("nextFrameIndex walks STICKMAN_FRAME_SEQUENCE and wraps around", () => {
  let idx = 0;
  for (let i = 1; i < STICKMAN_FRAME_SEQUENCE.length; i++) {
    const next = nextFrameIndex(idx);
    assert.equal(next.sequenceIndex, i);
    assert.equal(next.frame, STICKMAN_FRAME_SEQUENCE[i]);
    idx = next.sequenceIndex;
  }
  const wrapped = nextFrameIndex(idx);
  assert.equal(wrapped.sequenceIndex, 0);
  assert.equal(wrapped.frame, STICKMAN_FRAME_SEQUENCE[0]);
});

test("every frame index in the sequence is a real stickman sprite", () => {
  for (const f of STICKMAN_FRAME_SEQUENCE) {
    assert.ok(f >= 0 && f < STICKMAN_FRAMES.length, `frame ${f} out of range`);
  }
});

test("the stickman animates without ever touching the door/frame columns", () => {
  // The door + its frame occupy the left/centre of the grid; the stickman (and his
  // footprints) are confined to columns well to the right of it (user: he must stand
  // clearly away from the door, never cover it). Comparing two different frames: every
  // cell that differs must be in that right-hand region.
  const a = buildAjarGrid(STICKMAN_FRAME_SEQUENCE[0]);
  const b = buildAjarGrid(STICKMAN_FRAME_SEQUENCE[2]); // a different sprite
  const DOOR_AND_FRAME_MAX_COL = 60;
  let sawADifference = false;
  for (let row = 0; row < AJAR_ROWS; row++) {
    for (let col = 0; col <= DOOR_AND_FRAME_MAX_COL; col++) {
      const ca = a[row][col];
      const cb = b[row][col];
      const same = (ca === null && cb === null) || (ca !== null && cb !== null && ca.ch === cb.ch && ca.cls === cb.cls);
      assert.ok(same, `door/frame cell changed at row ${row} col ${col}`);
    }
    for (let col = DOOR_AND_FRAME_MAX_COL + 1; col < AJAR_COLS; col++) {
      const ca = a[row][col];
      const cb = b[row][col];
      if ((ca?.ch ?? " ") !== (cb?.ch ?? " ")) sawADifference = true;
    }
  }
  assert.ok(sawADifference, "expected the stickman region to actually change between frames");
});

test("footprints lead from the doorway towards where the stickman stopped", () => {
  const grid = buildAjarGrid(0);
  const lastRow = grid[AJAR_ROWS - 1];
  const footprintCols = lastRow
    .map((c, i) => (c && c.ch === "." && c.cls === "c-hueco" ? i : -1))
    .filter((i) => i >= 0);
  assert.ok(footprintCols.length >= 3, "expected several footprint dots on the floor row");
});

test("gridToHtml run-length-encodes classes and escapes HTML-sensitive characters", () => {
  const html = gridToHtml([
    [
      { ch: "<", cls: "c-q" },
      { ch: "&", cls: "c-q" },
      null,
      { ch: "x", cls: "c-man" },
    ],
  ]);
  assert.equal(html, '<span class="c-q">&lt;&amp;</span> <span class="c-man">x</span>');
});
