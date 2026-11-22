// T13.J4-fix (DA-13-J3J4-3): the mockup's own "done" row format (`Main.dc.html` line 541,
// `time: 'Completed 17 Nov 22:10'`) -- day, short month, HH:mm, UTC (consistent with the
// rest of the dashboard, which never shows a local-timezone time), deliberately no year and
// no seconds. `ErasurePanel.tsx` used to render the raw ISO wire value verbatim instead
// (`Completed 2026-11-17T22:10:00Z`) -- a genuine mockup-fidelity gap, not a cosmetic one.

const SHORT_MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

/** Formats an ISO instant as the mockup's own "17 Nov 22:10" shape (UTC). Falls back to the
 * raw input, unchanged, for anything that does not parse as a date -- never throws, never
 * fabricates a timestamp (same "never fabricate" rule `ErasurePanel.tsx`'s own
 * `timeCellText` already follows for a missing `completed_at`). */
export function formatCompletedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const day = date.getUTCDate();
  const month = SHORT_MONTHS[date.getUTCMonth()];
  const hh = String(date.getUTCHours()).padStart(2, "0");
  const mm = String(date.getUTCMinutes()).padStart(2, "0");
  return `${day} ${month} ${hh}:${mm}`;
}
