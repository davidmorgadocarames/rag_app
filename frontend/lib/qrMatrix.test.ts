import assert from "node:assert/strict";
import { test } from "node:test";

import { buildQrMatrix } from "./qrMatrix.ts";

// This is the test that would fail on the old code: before DEC-024 = B's QR task,
// `lib/qrMatrix.ts` did not exist and `/account/security` rendered no QR at all (manual
// secret/URI entry only).
const SAMPLE_URI =
  "otpauth://totp/SecRAG:alice@example.test?secret=JBSWY3DPEHPK3PXP&issuer=SecRAG&algorithm=SHA1&digits=6&period=30";

test("buildQrMatrix encodes a real otpauth:// URI into a non-trivial square matrix", () => {
  const { moduleCount, isDark } = buildQrMatrix(SAMPLE_URI);

  // A real QR matrix for a URI this long is at least 29x29 (version >= 3); this would be 0
  // (or throw) for an empty/placeholder implementation.
  assert.ok(moduleCount >= 29, `expected a real QR matrix, got moduleCount=${moduleCount}`);

  let darkCount = 0;
  for (let row = 0; row < moduleCount; row += 1) {
    for (let col = 0; col < moduleCount; col += 1) {
      if (isDark(row, col)) darkCount += 1;
    }
  }
  // A genuine QR code is neither all-light nor all-dark -- roughly half the modules are set.
  const total = moduleCount * moduleCount;
  assert.ok(darkCount > total * 0.2 && darkCount < total * 0.8, `darkCount=${darkCount}/${total}`);

  // The three fixed top-left finder-pattern corner modules (position marker) are always dark.
  assert.equal(isDark(0, 0), true);
});

test("buildQrMatrix is deterministic for the same input", () => {
  const a = buildQrMatrix(SAMPLE_URI);
  const b = buildQrMatrix(SAMPLE_URI);
  assert.equal(a.moduleCount, b.moduleCount);
  for (let row = 0; row < a.moduleCount; row += 1) {
    for (let col = 0; col < a.moduleCount; col += 1) {
      assert.equal(a.isDark(row, col), b.isDark(row, col));
    }
  }
});

test("buildQrMatrix produces a different matrix for a different secret", () => {
  const other = SAMPLE_URI.replace("JBSWY3DPEHPK3PXP", "AAAAAAAAAAAAAAAA");
  const a = buildQrMatrix(SAMPLE_URI);
  const b = buildQrMatrix(other);
  let differs = false;
  const moduleCount = Math.min(a.moduleCount, b.moduleCount);
  for (let row = 0; row < moduleCount && !differs; row += 1) {
    for (let col = 0; col < moduleCount && !differs; col += 1) {
      if (a.isDark(row, col) !== b.isDark(row, col)) differs = true;
    }
  }
  assert.ok(differs || a.moduleCount !== b.moduleCount, "expected the two matrices to differ");
});
