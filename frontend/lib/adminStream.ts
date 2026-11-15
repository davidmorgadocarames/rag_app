// Admin dashboard live updates (T13.4.1 SSE / T13.4.2 polling, TF14). Pure, DOM/EventSource-
// free logic only (C5 pattern): the actual `EventSource` wiring lives directly in the
// "use client" dashboard component (untested directly here, same reasoning as
// middleware.ts -- covered instead by the Playwright admin e2e spec), because jsdom is not
// part of this project's toolchain and the native browser `EventSource` has no meaningful
// Node equivalent to drive from `node --test`.
//
// The backend's SSE frames are a standard `event: <type>\ndata: <json>\n\n` shape
// (rag_app.api.admin._sse_frame) -- a real `EventSource` already demultiplexes "event:"
// into named listeners and hands each payload as a raw string through `.data`; this module
// only needs to parse that raw string safely.

export type AdminStreamEventType = "snapshot" | "heartbeat" | "closed";

export interface AdminStreamClosedReason {
  reason: string;
}

/** Safe `JSON.parse` of one SSE frame's `data:` payload -- `null` on anything malformed
 * (a backend bug or a transparent proxy mangling the stream must never crash the dashboard). */
export function parseAdminStreamData(raw: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** A human-readable banner message for a `closed` event's `reason` (T13.4.1): every case
 * tells the admin to reload, since the backend's own full page load re-proves auth/role/MFA
 * for real -- this module never tries to recover the stream itself. */
export function closedReasonMessage(reason: string): string {
  switch (reason) {
    case "session_expired":
      return "Your session has expired. Reload the page to sign in again.";
    case "role_lost":
      return "Your admin access has changed. Reload the page.";
    case "mfa_step_up_lost":
      return "Your two-factor step-up has expired. Reload the page and verify again.";
    case "idle_timeout":
      return "This live connection timed out after staying open a while. Reload to reconnect.";
    default:
      return "The live connection closed. Reload the page to reconnect.";
  }
}
