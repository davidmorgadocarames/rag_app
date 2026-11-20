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

export type WakeState = "idle" | "waking" | "failed";

export default function WakeBanner({
  state,
  onRetry,
  testId = "wake-banner",
}: {
  state: WakeState;
  onRetry?: () => void;
  testId?: string;
}) {
  if (state === "idle") return null;

  return (
    <p
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
          className="cursor-pointer border border-vault-steel-dark px-2.5 py-1 text-vault-amber hover:border-vault-steel"
        >
          Retry
        </button>
      )}
    </p>
  );
}
