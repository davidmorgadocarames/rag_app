import assert from "node:assert/strict";
import { test } from "node:test";

import {
  alertJumpFeatureId,
  countFeaturesByStatus,
  featureGroup,
  featureStatus,
  filterFeaturesByStatus,
  resolveAlertJump,
  sortFeaturesWorstFirst,
  TRIAGE_FILTERS,
  type TriageFeature,
} from "./adminTriage.ts";

function feature(partial: Partial<TriageFeature> & { id: string }): TriageFeature {
  return {
    probe_status: "ok",
    probe_detail: null,
    dev_only: false,
    azure_only: false,
    // Default false: the dev_only/azure_only fixtures below model the common "correctly off
    // by design" case unless a test explicitly says otherwise (DEBT-053's negative control).
    expected_here: false,
    ...partial,
  };
}

test("featureStatus: ok probe -> OK with the probe's own detail", () => {
  const info = featureStatus(feature({ id: "a", probe_status: "ok", probe_detail: "4 ms" }));
  assert.deepEqual(info, { key: "ok", label: "OK", detail: "4 ms", why: null });
});

test("featureStatus: no probe at all -> OK with a truthful config-check detail, never a bare ok", () => {
  const info = featureStatus(feature({ id: "a", probe_status: null, probe_detail: null }));
  assert.equal(info.key, "ok");
  assert.match(info.detail, /config check only/);
  assert.equal(info.why, null);
});

test("featureStatus: degraded -> Warning", () => {
  const info = featureStatus(
    feature({ id: "a", probe_status: "degraded", probe_detail: "p95 2.1s" }),
  );
  assert.equal(info.key, "warn");
  assert.equal(info.label, "Warning");
  assert.equal(info.detail, "p95 2.1s");
});

test("featureStatus: down and not dev_only/azure_only -> Critical, no why", () => {
  const info = featureStatus(feature({ id: "a", probe_status: "down", probe_detail: "SMTP failed" }));
  assert.equal(info.key, "crit");
  assert.equal(info.why, null);
});

test("featureStatus: down + dev_only -> Off here with an explanation, never unexplained", () => {
  const info = featureStatus(feature({ id: "lab", probe_status: "down", dev_only: true }));
  assert.equal(info.key, "off");
  assert.equal(info.label, "Off here");
  assert.ok(info.why && info.why.length > 0);
  assert.match(info.why as string, /design/);
});

test("featureStatus: down + azure_only -> Off here with a different, still truthful explanation", () => {
  const info = featureStatus(feature({ id: "az", probe_status: "down", azure_only: true }));
  assert.equal(info.key, "off");
  assert.match(info.why as string, /Azure/);
});

// T13.8.9 (DEBT-053, DA-13-G2-2): negative control -- a dev_only feature the admin
// explicitly turned ON here (expected_here: true, e.g. LAB_ENABLED=true locally) that is
// STILL down must be Critical, never masked as "Off here by design" just because it is
// conditional in general. Before this fix, the static dev_only check alone always won here.
test("featureStatus: down + dev_only BUT expected_here -> Critical, not Off here (negative control)", () => {
  const info = featureStatus(
    feature({ id: "lab", probe_status: "down", dev_only: true, expected_here: true }),
  );
  assert.equal(info.key, "crit");
  assert.equal(info.label, "Critical");
  assert.equal(info.why, null);
});

test("featureStatus: down + azure_only BUT expected_here -> Critical too", () => {
  const info = featureStatus(
    feature({ id: "az", probe_status: "down", azure_only: true, expected_here: true }),
  );
  assert.equal(info.key, "crit");
});

test("sortFeaturesWorstFirst: Critical, then Warning, then Off here, then OK", () => {
  const ok = feature({ id: "ok-1", probe_status: "ok" });
  const warn = feature({ id: "warn-1", probe_status: "degraded" });
  const crit = feature({ id: "crit-1", probe_status: "down" });
  const off = feature({ id: "off-1", probe_status: "down", dev_only: true });
  const sorted = sortFeaturesWorstFirst([ok, off, warn, crit]);
  assert.deepEqual(sorted.map((f) => f.id), ["crit-1", "warn-1", "off-1", "ok-1"]);
});

test("sortFeaturesWorstFirst never mutates its input", () => {
  const list = [feature({ id: "a", probe_status: "ok" }), feature({ id: "b", probe_status: "down" })];
  const original = [...list];
  sortFeaturesWorstFirst(list);
  assert.deepEqual(list, original);
});

test("countFeaturesByStatus counts every status key, including zero", () => {
  const counts = countFeaturesByStatus([
    feature({ id: "a", probe_status: "ok" }),
    feature({ id: "b", probe_status: "down" }),
    feature({ id: "c", probe_status: "down" }),
  ]);
  assert.deepEqual(counts, { crit: 2, warn: 0, off: 0, ok: 1 });
});

test("filterFeaturesByStatus 'all' returns every feature untouched", () => {
  const list = [feature({ id: "a", probe_status: "ok" }), feature({ id: "b", probe_status: "down" })];
  assert.deepEqual(filterFeaturesByStatus(list, "all"), list);
});

test("filterFeaturesByStatus narrows to exactly the matching status", () => {
  const list = [
    feature({ id: "a", probe_status: "ok" }),
    feature({ id: "b", probe_status: "down" }),
    feature({ id: "c", probe_status: "degraded" }),
  ];
  assert.deepEqual(
    filterFeaturesByStatus(list, "crit").map((f) => f.id),
    ["b"],
  );
  assert.deepEqual(
    filterFeaturesByStatus(list, "warn").map((f) => f.id),
    ["c"],
  );
});

test("TRIAGE_FILTERS covers all/crit/warn/off/ok, each exactly once", () => {
  assert.deepEqual(
    TRIAGE_FILTERS.map((f) => f.key),
    ["all", "crit", "warn", "off", "ok"],
  );
});

test("featureGroup derives the convention prefix before the first hyphen", () => {
  assert.equal(featureGroup("identity-mfa"), "identity");
  assert.equal(featureGroup("rag-rerank"), "rag");
  assert.equal(featureGroup("admin-defence-lab"), "admin");
});

test("featureGroup falls back to the whole id when there is no hyphen", () => {
  assert.equal(featureGroup("standalone"), "standalone");
});

test("alertJumpFeatureId picks the first feature_id that is actually a known feature", () => {
  const features = [{ id: "identity-email-verification-reset" }, { id: "rag-rerank" }];
  const id = alertJumpFeatureId(
    { feature_ids: ["unknown-id", "identity-email-verification-reset"] },
    features,
  );
  assert.equal(id, "identity-email-verification-reset");
});

test("alertJumpFeatureId returns null when no feature_ids match, never a guess", () => {
  const features = [{ id: "rag-rerank" }];
  assert.equal(alertJumpFeatureId({ feature_ids: ["unknown-id"] }, features), null);
  assert.equal(alertJumpFeatureId({}, features), null);
});

// T13.8.7 (DEC-043 = A, DA-13-G2-1): regression tests using the REAL `feature_ids` shape
// `_banner_alerts` (admin.py) actually sends for these two codes -- both carry the real
// registry id "privacy-asynchronous-erasure", which is exactly why the naive
// `alertJumpFeatureId`-first order used to always win and never fall back to "Open erasure".
const REAL_ERASURE_FEATURES = [{ id: "privacy-asynchronous-erasure" }, { id: "rag-rerank" }];

test("resolveAlertJump: erasure_overdue always targets the Erasure section, never the feature it also names", () => {
  const target = resolveAlertJump(
    { code: "erasure_overdue", feature_ids: ["privacy-asynchronous-erasure"] },
    REAL_ERASURE_FEATURES,
  );
  assert.deepEqual(target, { featureId: null, section: "erasure" });
});

test("resolveAlertJump: erasure_approaching always targets the Erasure section too", () => {
  const target = resolveAlertJump(
    { code: "erasure_approaching", feature_ids: ["privacy-asynchronous-erasure"] },
    REAL_ERASURE_FEATURES,
  );
  assert.deepEqual(target, { featureId: null, section: "erasure" });
});

test("resolveAlertJump: a non-erasure code still resolves its own feature_ids as before", () => {
  const target = resolveAlertJump(
    { code: "daily_cap_reached", feature_ids: ["unknown-id", "rag-rerank"] },
    REAL_ERASURE_FEATURES,
  );
  assert.deepEqual(target, { featureId: "rag-rerank", section: null });
});

test("resolveAlertJump: a non-erasure code with no matching feature_ids resolves neither", () => {
  const target = resolveAlertJump({ code: "lockout_spike", feature_ids: ["unknown-id"] }, [
    { id: "rag-rerank" },
  ]);
  assert.deepEqual(target, { featureId: null, section: null });
});
