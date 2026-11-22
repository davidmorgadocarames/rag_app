// Shared "waking the server up" UI (T13.9.1, DEBT-056, DEC-048): the one place every page that
// uses lib/wakeUp.ts's retry policy (LoginPanel, ChatPage, AdminDashboard) renders its state --
// never a per-page copy. Reuses the existing vault tokens (amber = informational/in-progress,
// matching the chat page's own stage labels; --danger-text = the SAME tone the rest of the app
// uses for a real error, once the retry budget is exhausted -- still clearly worded as a wake-
// up issue, never confused with e.g. a wrong password).
//
// Accessibility: `role="status"` + `aria-live="polite"` (T13.9.1's own requirement) announces
// the state change without stealing focus; the Retry button is a plain, keyboard-reachable
// <button>.
//
// DA-13-H-1 fix (13-H-fix): `retrying` is the caller's own in-flight flag (backed by a
// lib/retryGuard.ts ref, never only React state -- see each caller for why). While true the
// button is `disabled` (the SAME pattern the primary Send/ENTER button already uses for
// `busy`) and its label changes to "Retrying…" -- announced by the SAME `aria-live="polite"`
// region as the rest of the banner, so a screen reader hears the state change without a
// separate live region. This is a belt-and-braces UI affordance: the actual double-submit
// fix is the synchronous ref guard in the caller, which blocks a duplicate call even for two
// activations in the same tick, before this `disabled` attribute could possibly have committed.

import { WAKE_STEP_LABELS, WAKE_STEP_ORDER, type WakeStepsState } from "@/lib/wakeSteps";

export type WakeState = "idle" | "waking" | "failed";

export default function WakeBanner({
  state,
  onRetry,
  retrying = false,
  testId = "wake-banner",
  steps,
  elapsedSeconds = 0,
}: {
  state: WakeState;
  onRetry?: () => void;
  retrying?: boolean;
  testId?: string;
  // T13.12.6 (DEBT-062): the real pipeline sub-steps, listed under the existing text in the
  // SAME box -- CHAT ONLY (`undefined` everywhere else: LoginPanel/AdminDashboard keep the
  // plain banner, unchanged, by simply never passing this prop).
  steps?: WakeStepsState;
  elapsedSeconds?: number;
}) {
  if (state === "idle") return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid={testId}
      className={`flex flex-wrap items-center gap-3 border p-2.5 text-xs ${
        state === "failed" ? "border-vault-danger text-vault-danger-text" : "border-vault-amber text-vault-amber"
      }`}
    >
      <span className="animate-pulse">●</span>
      <span className="flex-1">
        {state === "waking"
          ? "Waking the server up… this can take up to a minute after a quiet period."
          : "The server is taking longer than usual to wake up."}
      </span>
      {state === "failed" && onRetry && (
        <button
          type="button"
          data-testid={`${testId}-retry`}
          onClick={onRetry}
          disabled={retrying}
          aria-disabled={retrying}
          className="cursor-pointer border border-vault-steel-dark px-2.5 py-1 text-vault-amber hover:border-vault-steel disabled:cursor-not-allowed disabled:opacity-50"
        >
          {retrying ? "Retrying…" : "Retry"}
        </button>
      )}
      {steps && state === "waking" && (
        <ul
          data-testid={`${testId}-steps`}
          className="flex w-full flex-col gap-0.5 border-t border-vault-steel-dark pt-2"
        >
          {WAKE_STEP_ORDER.map((id) => {
            const status = steps.statuses[id];
            return (
              <li
                key={id}
                data-testid={`${testId}-step-${id}`}
                data-status={status}
                className={
                  status === "current"
                    ? "text-vault-amber"
                    : status === "done"
                      ? "text-vault-steel-light"
                      : "text-vault-steel-dark"
                }
              >
                <span aria-hidden="true">{status === "done" ? "✓ " : status === "current" ? "▸ " : "· "}</span>
                {WAKE_STEP_LABELS[id]}
                {status === "current" && (
                  // Elapsed seconds tick every second (lib/useWakeSteps.ts) -- `aria-hidden`
                  // so a screen reader's live-region announcement reacts to a STEP CHANGE
                  // only, never a per-second mutation (T13.12.6's "no announcement spam").
                  <span aria-hidden="true" className="ml-1 text-vault-steel-dark">
                    ({elapsedSeconds}s)
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
