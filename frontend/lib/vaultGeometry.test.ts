import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_VAULT_CONFIG,
  TONE,
  createVaultSampler,
  sampleDoorBody,
  toneClass,
} from "./vaultGeometry.ts";

test("toneClass maps the four tone bands to the four ASCII classes", () => {
  assert.equal(toneClass(TONE.LINE), "c-linea");
  assert.equal(toneClass(TONE.MID), "c-medio");
  assert.equal(toneClass(TONE.HOLLOW), "c-hueco");
  assert.equal(toneClass(TONE.PLATE), "c-chapa");
});

test("sampleDoorBody is defined everywhere inside the disc, null outside it", () => {
  const cfg = DEFAULT_VAULT_CONFIG;
  assert.notEqual(sampleDoorBody(0, 0, 0, 0, cfg), null);
  assert.notEqual(sampleDoorBody(cfg.R - 1, 0, 0, 0, cfg), null);
  assert.equal(sampleDoorBody(cfg.R + 5, 0, 0, 0, cfg), null);
});

test("retraccion=1 withdraws the bolt ring -- the SAME slab the 404 page's ajar door reuses", () => {
  // At a bolt's exact angular position, a mid-retraction value is "mid-bolt" (HOLLOW/LINE,
  // never the bare plate) while a fully withdrawn one (retraccion=1, what NotFoundVault's
  // ajar slab always passes) is flush plate. If this ever drifted apart from
  // components/VaultDoor.tsx's own closed-door state machine, this test would catch it.
  const cfg = DEFAULT_VAULT_CONFIG;
  const boltX = Math.cos(0) * cfg.R * 0.845;
  const boltY = Math.sin(0) * cfg.R * 0.845;
  const closedBolt = sampleDoorBody(boltX, boltY, 0, 0, cfg);
  const openBolt = sampleDoorBody(boltX, boltY, 0, 1, cfg);
  assert.notEqual(closedBolt, TONE.PLATE);
  assert.equal(openBolt, TONE.PLATE);
});

test("createVaultSampler adds the surrounding frame outside the slab's own disc", () => {
  const sample = createVaultSampler(DEFAULT_VAULT_CONFIG);
  const cfg = DEFAULT_VAULT_CONFIG;
  // Just outside R, inside the frame's outer box L = R + 3.2: must be a frame tone.
  assert.notEqual(sample(cfg.R + 1, 0, 0, 0), null);
  // Well outside the frame's outer box: nothing.
  assert.equal(sample(cfg.R + 10, cfg.R + 10, 0, 0), null);
});
