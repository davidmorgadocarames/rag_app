// Shared ASCII vault-door geometry (T13.10.1): `components/VaultDoor.tsx` (closed door, the
// login/landing hero) and `components/NotFoundVault.tsx` (door swung open, the 404 page) both
// render the exact same wheel/bolt/plate formula -- this module is the ONE place it lives, so
// the two pages' art can never silently drift apart into two hand-tuned copies.
//
// `sampleDoorBody` is the slab itself (wheel + bolt ring + seam), valid only inside the disc
// r <= cfg.R; it always returns a tone there (the bare-plate `TONE.PLATE` is its own default).
// `createVaultSampler` wraps it with the surrounding square frame for the CLOSED door's full
// view (`VaultDoor.tsx`'s `giro`/`retraccion` state machine). The 404 page instead calls
// `sampleDoorBody` directly with `retraccion = 1` (bolts fully withdrawn -- the door is
// already unlocked and standing open) on its OWN local slab coordinates, swung out of a
// frame it draws itself (`vaultAjarScene.ts`) -- same slab formula, different composition,
// never a second copy of the formula.

export const TONE = { LINE: 0.99, MID: 0.66, HOLLOW: 0.42, PLATE: 0.14 } as const;

export function toneClass(v: number): string {
  if (v >= 0.9) return "c-linea";
  if (v >= 0.55) return "c-medio";
  if (v >= 0.3) return "c-hueco";
  return "c-chapa";
}

export interface DoorConfig {
  R: number;
  nPernos: number;
  nRadios: number;
  rLlanta: number;
  junta: number;
}

export type SampleFn = (x: number, y: number, giro: number, retraccion: number) => number | null;

// The locking wheel + its spokes/knobs (a bolt pattern of its own, independent of the main
// door's `nPernos` bolt ring below).
function sampleWheel(
  x: number,
  y: number,
  giro: number,
  retraccion: number,
  cfg: DoorConfig,
): number | null {
  const r = Math.hypot(x, y);
  const th = Math.atan2(y, x);
  const rHub = cfg.R * 0.17;
  const rLlanta = cfg.R * cfg.rLlanta;
  const paso = (2 * Math.PI) / cfg.nRadios;
  const k = Math.round((th - giro) / paso);
  const ang = giro + k * paso;
  const knob = 1.5 * (1 - retraccion * 0.35);

  const kx = Math.cos(ang) * (rLlanta + knob * 0.55);
  const ky = Math.sin(ang) * (rLlanta + knob * 0.55);
  const dk = Math.hypot(x - kx, y - ky);
  if (dk < knob) return dk > knob - 0.6 ? TONE.LINE : TONE.MID;

  if (r > rLlanta + 0.7) return null;
  if (Math.abs(r - rLlanta) < 0.7) return TONE.LINE;
  if (r <= rHub) return rHub - r < 0.7 ? TONE.LINE : TONE.HOLLOW;
  const dth = ((th - ang + Math.PI) % (2 * Math.PI)) - Math.PI;
  if (Math.abs(r * dth) < 1.15) return Math.abs(r * dth) > 0.3 ? TONE.LINE : TONE.MID;
  return null;
}

// The slab: wheel + bolt ring + seam. Valid only for r <= cfg.R (null outside, so callers can
// compose a frame/background around it); bolts retract smoothly as `retraccion` goes 0 -> 1.
export function sampleDoorBody(
  x: number,
  y: number,
  giro: number,
  retraccion: number,
  cfg: DoorConfig,
): number | null {
  const r = Math.hypot(x, y);
  const th = Math.atan2(y, x);

  const wheel = sampleWheel(x, y, giro, retraccion, cfg);
  if (wheel !== null) return wheel;

  if (r > cfg.R) return null;
  if (cfg.R - r < 0.75) return TONE.LINE;
  if (Math.abs(r - cfg.R * cfg.junta) < 0.42) return TONE.LINE;

  const paso = (2 * Math.PI) / cfg.nPernos;
  const k = Math.round(th / paso);
  const bolt = 1.0 * (1 - retraccion);
  const px = Math.cos(k * paso) * cfg.R * 0.845;
  const py = Math.sin(k * paso) * cfg.R * 0.845;
  const dp = Math.hypot(x - px, y - py);
  if (bolt > 0.02 && dp < bolt) return dp > bolt * 0.55 ? TONE.LINE : TONE.HOLLOW;
  return TONE.PLATE;
}

// Closed door (slab + its surrounding square frame): VaultDoor.tsx's own full sampler.
export function createVaultSampler(cfg: DoorConfig): SampleFn {
  return function sample(x: number, y: number, giro: number, retraccion: number): number | null {
    const r = Math.hypot(x, y);
    const body = sampleDoorBody(x, y, giro, retraccion, cfg);
    if (body !== null) return body;

    const L = cfg.R + 3.2;
    if (Math.max(Math.abs(x), Math.abs(y)) <= L) {
      if (Math.max(Math.abs(x), Math.abs(y)) > L - 0.8 || r < cfg.R + 1.1) return TONE.LINE;
      return TONE.MID;
    }
    return null;
  };
}

export const DEFAULT_VAULT_CONFIG: DoorConfig = {
  R: 13.5,
  nPernos: 12,
  nRadios: 4,
  rLlanta: 0.52,
  junta: 0.68,
};

// Real font measurement: derive the cell aspect ratio from the box a character actually
// occupies in THIS browser/font, instead of assuming one -- shared by VaultDoor.tsx and
// NotFoundVault.tsx so both ASCII scenes stay round under any monospace font substitution.
export function measureCellAspect(pre: HTMLElement): number {
  const probe = document.createElement("span");
  probe.textContent = "MMMMMMMMMM";
  probe.style.visibility = "hidden";
  pre.appendChild(probe);
  const rect = probe.getBoundingClientRect();
  pre.removeChild(probe);
  if (rect.width > 0 && rect.height > 0) return rect.width / 10 / rect.height;
  return 0.5;
}
