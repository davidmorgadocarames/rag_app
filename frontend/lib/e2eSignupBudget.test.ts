// DA-14-D2-2 (ride-along, block 14-D3): the e2e signup-velocity budget
// (`rag_app.risk.SignupTracker`, 5/hour/IP, `scripts/gate.sh`'s `step_e2e` runs every spec
// against ONE backend process) was already silently re-tripped once (14-D2's own
// `plan-panel.spec.ts` becoming the 6th real registration, fixed in 14-D2-fix) before
// anyone noticed -- the first time `gate.sh --full` actually ran it. This test turns
// "nobody noticed until a gate run failed" into "the gate catches it the moment a NEW spec
// adds a call", the same precedent DEC-039's `_DOD_GRANDFATHERED_IDS` size-pin already set
// for a different silent-opt-in risk: the files below are the REVIEWED, already-counted set
// (DA-14-D2.md's own tally); adding another file to `ALLOWED_FILES` is a deliberate,
// visible one-line PR diff, never a silent accident.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";

const E2E_DIR = path.join(import.meta.dirname, "..", "e2e");

// Only a REAL CALL counts -- `registerAndReachChat(` -- never a comment or import line that
// merely mentions the name (e.g. plan-panel.spec.ts's own explanatory comment, left behind
// by 14-D2-fix, about why it no longer calls this helper).
const CALL_RE = /registerAndReachChat\s*\(/;

// Reviewed, already-counted against the 5/hour/IP budget (DA-14-D2.md's own tally):
// admin.spec.ts (needs a FRESH account to promote to admin -- cannot reuse a seeded one),
// mfa.spec.ts (needs a fresh account to enrol MFA on), security-headers.spec.ts (walks a
// registration page for CSP violations). auth.spec.ts itself registers INLINE (its own
// form fill + click, never through this shared helper -- see its own two tests) and so is
// correctly absent from this list, not an oversight. plan-panel.spec.ts USED to call
// registerAndReachChat too (14-D2) -- migrated OFF it by 14-D2-fix specifically because
// adding it as a 6th call tripped the budget; it must never be added back to this list
// without first re-checking the budget has headroom.
const ALLOWED_FILES: ReadonlySet<string> = new Set([
  "admin.spec.ts",
  "mfa.spec.ts",
  "security-headers.spec.ts",
]);

function specFiles(): string[] {
  return readdirSync(E2E_DIR).filter((f) => f.endsWith(".spec.ts"));
}

test("only the reviewed, already-counted spec files call registerAndReachChat", () => {
  const offenders: string[] = [];
  for (const file of specFiles()) {
    if (ALLOWED_FILES.has(file)) continue;
    const text = readFileSync(path.join(E2E_DIR, file), "utf8");
    if (CALL_RE.test(text)) offenders.push(file);
  }
  assert.deepEqual(
    offenders,
    [],
    "a NEW spec calls registerAndReachChat outside the reviewed ALLOWED_FILES list -- this" +
      " spends more of rag_app.risk.SignupTracker's 5/hour/IP signup-velocity budget inside" +
      " scripts/gate.sh's ONE shared backend process (DA-14-D2-2). Use loginAndReachChat" +
      " against a seeded e2e account instead (see plan-panel.spec.ts / 14-D2-fix), or add" +
      " this file to ALLOWED_FILES here ONLY after re-checking the budget has headroom.",
  );
});

test("the allow-list names only files that still exist and still really call it (no stale entries)", () => {
  const files = new Set(specFiles());
  for (const allowed of ALLOWED_FILES) {
    assert.ok(files.has(allowed), `${allowed} is in ALLOWED_FILES but no longer exists`);
    const text = readFileSync(path.join(E2E_DIR, allowed), "utf8");
    assert.ok(
      CALL_RE.test(text),
      `${allowed} is in ALLOWED_FILES but no longer calls registerAndReachChat -- remove it`,
    );
  }
});

// Negative control (same discipline as lib/adminTailwindCompile.test.ts's own one): proves
// the detection regex itself actually distinguishes a real call from a mere mention, rather
// than this whole check passing vacuously because the pattern never matches anything.
test("the detection regex matches a real call but not a comment merely naming the helper", () => {
  assert.ok(CALL_RE.test('await registerAndReachChat(page, email, password);'));
  assert.ok(CALL_RE.test("registerAndReachChat(page, email, password)"));
  assert.ok(
    !CALL_RE.test(
      "// 14-D2's original version used `registerAndReachChat`, migrated off it below",
    ),
  );
  assert.ok(!CALL_RE.test('import { registerAndReachChat } from "./helpers.ts";'));
});
