// Pure scene builder for the 404 page's ASCII illustration (T13.10.1, DEBT-057): the vault
// door standing AJAR, with a confused stickman who has walked out of it and stopped well
// clear of the frame, plus his footprints. Next.js-import-free (C5 pattern, like
// lib/authGuard.ts/lib/csp.ts) so `node --test` can exercise the grid directly; the thin
// client component (components/NotFoundVault.tsx) only measures the font and re-renders on
// an interval/resize.
//
// The door slab reuses `sampleDoorBody` from lib/vaultGeometry.ts -- the SAME formula
// `components/VaultDoor.tsx` draws its closed door with (T13.10.1's "no copy-pasted
// crearPuerta" requirement) -- called here with `retraccion = 1` (bolts fully withdrawn: the
// door is already unlocked) on the slab's own local coordinates, then swung out of a frame
// this module draws itself. The frame/interior/footprints/stickman composition is specific
// to this page and lives only here.
import { DEFAULT_VAULT_CONFIG, sampleDoorBody, toneClass, type DoorConfig } from "./vaultGeometry.ts";

export const AJAR_COLS = 96;
export const AJAR_ROWS = 31;

const SLAB_CONFIG: DoorConfig = { ...DEFAULT_VAULT_CONFIG, R: 10.5 };
const SLAB_GIRO = 0.35; // a fixed wheel angle -- this illustration is not spinning
const SWING = 0.36; // fraction of 2R the slab has swung open, seen from the front
const CENTER_COL = 38;
const ASPECT_FALLBACK = 0.55;

// Three frames: scratching his head / shrug / "??" -- cycled by the component on an
// interval, never on the door or frame (those never change).
export const STICKMAN_FRAMES: readonly string[][] = [
  [
    "      ?       ",
    "    .---.  _  ",
    "   ( o O )/ \\ ",
    "    '---' | | ",
    "      |  _/ / ",
    "     /|_/     ",
    "    / |       ",
    "      |       ",
    "     / \\      ",
    "    /   \\     ",
    "   ~     ~    ",
  ],
  [
    "  ?           ",
    "    .---.     ",
    "   ( O o )    ",
    "    '---'     ",
    "  \\_  |  _/   ",
    "    \\_|_/     ",
    "      |       ",
    "      |       ",
    "     / \\      ",
    "    /   \\     ",
    "   ~     ~    ",
  ],
  [
    "        ? ?   ",
    "    .---.  _  ",
    "   ( O O )/ \\ ",
    "    '---' | | ",
    "      |  _/ / ",
    "     /|_/     ",
    "    / |       ",
    "      |       ",
    "     / \\      ",
    "    /   \\     ",
    "   ~     ~    ",
  ],
];

// Idle sequence: lingers on each frame (the component steps through this array on its own
// fixed-interval timer; indices, not milliseconds, so the timing policy lives in one place).
export const STICKMAN_FRAME_SEQUENCE: readonly number[] = [0, 0, 1, 1, 2, 0, 1];
export const STICKMAN_FRAME_INTERVAL_MS = 850;

// Well clear of the frame (whose right edge sits near col CENTER_COL + (R+3.2)/aspect): he
// has walked out and stopped -- never covering the door.
const MAN_COL = 76;
const MAN_ROW = AJAR_ROWS - 11;
const FOOTPRINTS_FROM_COL = 65;

export function nextFrameIndex(sequenceIndex: number): { sequenceIndex: number; frame: number } {
  const next = (sequenceIndex + 1) % STICKMAN_FRAME_SEQUENCE.length;
  return { sequenceIndex: next, frame: STICKMAN_FRAME_SEQUENCE[next] };
}

// prefers-reduced-motion: callers must never start the interval at all -- a single static
// frame (frame 0) is drawn once and left alone.
export function shouldAnimate(prefersReducedMotion: boolean): boolean {
  return !prefersReducedMotion;
}

export interface SceneCell {
  ch: string;
  cls: string;
}

function toneToCell(v: number): SceneCell {
  return { ch: "░▒▓█"[Math.min(3, Math.floor(v * 4))], cls: toneClass(v) };
}

function slabCell(localX: number, localY: number): SceneCell | null {
  const v = sampleDoorBody(localX, localY, SLAB_GIRO, 1, SLAB_CONFIG);
  return v === null ? null : toneToCell(v);
}

function frameAndInteriorCell(x: number, y: number, col: number, row: number): SceneCell | null {
  const r = Math.hypot(x, y);
  const L = SLAB_CONFIG.R + 3.2;
  const m = Math.max(Math.abs(x), Math.abs(y));

  if (r < SLAB_CONFIG.R + 1.1 && r > SLAB_CONFIG.R - 0.2) return { ch: "█", cls: "c-linea" };
  if (r <= SLAB_CONFIG.R - 0.2) {
    // Dark interior; a faint amber glow spills out low on the right.
    const glow = x > 2 && y < -2 && (col + row) % 3 === 0;
    return glow ? { ch: ".", cls: "c-hueco" } : { ch: "░", cls: "c-dark" };
  }
  if (m <= L) return m > L - 0.8 ? { ch: "█", cls: "c-linea" } : { ch: "▓", cls: "c-medio" };
  return null;
}

function cell(col: number, row: number, aspect: number): SceneCell | null {
  const x = (col - CENTER_COL + 0.5) * aspect;
  const y = AJAR_ROWS / 2 - row - 0.5;

  // 1) The slab, swung open towards the viewer on a hinge at the left edge of the opening.
  if (x <= -SLAB_CONFIG.R && x >= -SLAB_CONFIG.R - 2 * SLAB_CONFIG.R * SWING) {
    const u = (-SLAB_CONFIG.R - x) / SWING; // distance from the hinge along the slab
    const slab = slabCell(SLAB_CONFIG.R - u, y);
    if (slab !== null) return slab;
  }

  // 2) The frame around the opening + the dark interior behind it.
  return frameAndInteriorCell(x, y, col, row);
}

function placeFootprintsAndStickman(grid: (SceneCell | null)[][], frame: number): void {
  for (let fp = FOOTPRINTS_FROM_COL; fp < MAN_COL + 2; fp += 3) {
    grid[AJAR_ROWS - 1][fp] = { ch: ".", cls: "c-hueco" };
  }
  const sprite = STICKMAN_FRAMES[frame];
  for (let i = 0; i < sprite.length; i++) {
    const s = sprite[i];
    const first = s.search(/\S/);
    let last = s.length - 1;
    while (last >= 0 && s[last] === " ") last--;
    for (let j = Math.max(0, first - 1); j <= last + 1 && j < s.length + 1; j++) {
      const c = MAN_COL + j;
      const r = MAN_ROW + i;
      if (c >= AJAR_COLS || r >= AJAR_ROWS || c < 0) continue;
      const ch = s[j] ?? " ";
      grid[r][c] = ch === " " ? null : { ch, cls: ch === "?" ? "c-q" : "c-man" };
    }
  }
}

// Builds the full grid for one frame -- pure, no DOM. `aspect` is the measured monospace
// cell width/height ratio (falls back to a sane default so this is also usable headless).
export function buildAjarGrid(frame: number, aspect = ASPECT_FALLBACK): (SceneCell | null)[][] {
  const grid: (SceneCell | null)[][] = [];
  for (let row = 0; row < AJAR_ROWS; row++) {
    const line: (SceneCell | null)[] = [];
    for (let col = 0; col < AJAR_COLS; col++) line.push(cell(col, row, aspect));
    grid.push(line);
  }
  placeFootprintsAndStickman(grid, frame);
  return grid;
}

// Renders a grid to the same run-length-encoded `<span class="…">…</span>` HTML VaultDoor.tsx
// uses -- cheap to diff/assert on in tests, and what the component assigns to `pre.innerHTML`.
export function gridToHtml(grid: (SceneCell | null)[][]): string {
  const rowsHtml: string[] = [];
  for (const row of grid) {
    let html = "";
    let cur: string | null = null;
    let run = "";
    const flush = () => {
      if (run) html += cur ? `<span class="${cur}">${run}</span>` : run;
      run = "";
    };
    for (const g of row) {
      const cls = g ? g.cls : null;
      const ch = g ? g.ch : " ";
      if (cls !== cur) {
        flush();
        cur = cls;
      }
      run += ch === "<" ? "&lt;" : ch === ">" ? "&gt;" : ch === "&" ? "&amp;" : ch;
    }
    flush();
    rowsHtml.push(html);
  }
  return rowsHtml.join("\n");
}
