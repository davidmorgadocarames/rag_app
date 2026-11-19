// T13.8.5/T13.8.6 (13-G2): pure, DOM-free triage logic (C5 pattern, same as adminStream.ts) --
// the status mapping, worst-first sort, filter/count and alert-jump rules the new Triage
// section and feature drawer both need, kept directly unit-testable.

// A structural subset of `FeatureHealthOut` (lib/api.ts) -- callers pass the real type, this
// module only reads the fields it actually needs.
export interface TriageFeature {
  id: string;
  probe_status: string | null;
  probe_detail: string | null;
  dev_only: boolean;
  azure_only: boolean;
}

export type FeatureStatusKey = "crit" | "warn" | "off" | "ok";

export interface FeatureStatusInfo {
  key: FeatureStatusKey;
  label: string;
  detail: string;
  // Non-null only for `key === "off"`: the truthful explanation for why this feature being
  // down HERE is expected, never an unexplained failure.
  why: string | null;
}

const STATUS_LABEL: Record<FeatureStatusKey, string> = {
  crit: "Critical",
  warn: "Warning",
  off: "Off here",
  ok: "OK",
};

// Worst first: Critical, then Warning, then Off here (neutral-by-design), then OK.
const STATUS_RANK: Record<FeatureStatusKey, number> = { crit: 0, warn: 1, off: 2, ok: 3 };

function offHereWhy(feature: TriageFeature): string {
  if (feature.dev_only) {
    return (
      "Expected: this feature only runs with dev-only routes enabled (e.g. the defence lab). " +
      "It is off on this stack by design, not a failure."
    );
  }
  return (
    "Expected: this feature only runs on Azure. It is off on this (non-Azure) stack by " +
    "design, not a failure."
  );
}

/** Maps a feature's raw probe outcome to the triage vocabulary (T13.8.5): `ok`/`degraded` ->
 * OK/Warning; no probe at all -> OK with a truthful "config check only" detail (never silently
 * "ok" as if a real probe ran); `down` -> Critical, EXCEPT a `dev_only`/`azure_only` feature
 * that is down BY DESIGN on this stack -> "Off here" with an explanation (replaces the old,
 * unexplained "N probe(s) not ok"). */
export function featureStatus(feature: TriageFeature): FeatureStatusInfo {
  const { probe_status, probe_detail } = feature;
  if (probe_status === null) {
    return {
      key: "ok",
      label: STATUS_LABEL.ok,
      detail: probe_detail ?? "No automated probe for this feature -- config check only.",
      why: null,
    };
  }
  if (probe_status === "ok") {
    return { key: "ok", label: STATUS_LABEL.ok, detail: probe_detail ?? "probe ok", why: null };
  }
  if (probe_status === "degraded") {
    return {
      key: "warn",
      label: STATUS_LABEL.warn,
      detail: probe_detail ?? "degraded",
      why: null,
    };
  }
  // "down" (or any other value the backend might one day add -- treated as down, never as ok).
  if (feature.dev_only || feature.azure_only) {
    return {
      key: "off",
      label: STATUS_LABEL.off,
      detail: probe_detail ?? "off by design on this stack",
      why: offHereWhy(feature),
    };
  }
  return { key: "crit", label: STATUS_LABEL.crit, detail: probe_detail ?? "down", why: null };
}

/** A new array, worst feature first (Critical, Warning, Off here, OK); stable within the same
 * status so two features of equal severity keep the registry's own relative order. */
export function sortFeaturesWorstFirst<T extends TriageFeature>(features: T[]): T[] {
  return [...features].sort(
    (a, b) => STATUS_RANK[featureStatus(a).key] - STATUS_RANK[featureStatus(b).key],
  );
}

export type TriageFilterKey = "all" | "crit" | "warn" | "off" | "ok";

export const TRIAGE_FILTERS: { key: TriageFilterKey; label: string }[] = [
  { key: "all", label: "All" },
  { key: "crit", label: "Critical" },
  { key: "warn", label: "Warning" },
  { key: "off", label: "Off here" },
  { key: "ok", label: "OK" },
];

export function countFeaturesByStatus<T extends TriageFeature>(
  features: T[],
): Record<FeatureStatusKey, number> {
  const counts: Record<FeatureStatusKey, number> = { crit: 0, warn: 0, off: 0, ok: 0 };
  for (const feature of features) counts[featureStatus(feature).key] += 1;
  return counts;
}

export function filterFeaturesByStatus<T extends TriageFeature>(
  features: T[],
  filter: TriageFilterKey,
): T[] {
  if (filter === "all") return features;
  return features.filter((feature) => featureStatus(feature).key === filter);
}

/** The registry id convention is `<group>-<rest>` (e.g. `identity-mfa`, `rag-rerank`) -- a
 * truthful, derived grouping label for the triage row's trailing column, never a second,
 * hand-maintained lookup table that could drift from the real ids. */
export function featureGroup(featureId: string): string {
  const i = featureId.indexOf("-");
  return i === -1 ? featureId : featureId.slice(0, i);
}

// --- alert jump action (T13.8.4/T13.8.6) --------------------------------------------------

export interface JumpableAlert {
  feature_ids?: string[];
}

/** The feature id an alert's "Open feature" action should select, i.e. the first of its own
 * `feature_ids` that is actually a known registry feature here -- `null` when the alert names
 * no feature we know about (never a guess, and never a stale/mismatched id). */
export function alertJumpFeatureId(
  alert: JumpableAlert,
  features: { id: string }[],
): string | null {
  const ids = alert.feature_ids ?? [];
  const known = new Set(features.map((f) => f.id));
  for (const id of ids) {
    if (known.has(id)) return id;
  }
  return null;
}

export interface AlertJumpTarget {
  featureId: string | null;
  section: "erasure" | null;
}

/** T13.8.7 (DEC-043 = A, DA-13-G2-1, high): the alert banner's own jump action for
 * `erasure_overdue`/`erasure_approaching` -- BOTH carry a real registry id
 * (`feature_ids: ["privacy-asynchronous-erasure"]`, set in `_banner_alerts`, 13-G1), so
 * `alertJumpFeatureId` alone would always resolve a feature first and never fall back to the
 * Erasure section the Done-when and the approved mockup actually want for these two codes.
 * This resolver checks the erasure-shaped code FIRST: it always sends those two codes to the
 * Erasure section, regardless of `feature_ids` -- `feature_ids` itself is untouched and still
 * used by the feature drawer's own "related alerts" list (`FeatureDrawer`'s `relatedAlerts`
 * filter reads the alert's `feature_ids` directly, not this resolver). Any other alert code
 * keeps the original feature-first behaviour. */
export function resolveAlertJump(
  alert: JumpableAlert & { code: string },
  features: { id: string }[],
): AlertJumpTarget {
  if (alert.code.startsWith("erasure")) {
    return { featureId: null, section: "erasure" };
  }
  return { featureId: alertJumpFeatureId(alert, features), section: null };
}
