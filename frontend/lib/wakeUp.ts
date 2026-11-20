// Wake-up UX after scale-to-zero (T13.9.1, DEBT-056, DEC-048): a single shared retry/backoff
// mechanism for every BFF/`/api/*` call, instead of a per-page copy, so the policy and the
// idempotency reasoning live in exactly one place.
//
// Azure runs the 3 Container Apps with minReplicas 0 -- after an idle cooldown the backend is
// scaled to zero and the next request hits a cold start. What the BFF (lib/bffProxy.ts)
// actually returns in that case, by construction:
//   - the backend has no replica yet and the connect itself fails (no process is listening) ->
//     the BFF's own `fetch` throws BEFORE any byte of the request could have been sent -> the
//     BFF's catch block returns 502 with the exact body `{"detail":"upstream unreachable"}`.
//     This is the common scale-from-zero signature: provably safe to retry, even for a
//     non-idempotent call, because nothing ever reached the backend.
//   - the connect succeeds (a replica is booting/warm) but the full round trip takes longer
//     than the BFF's own abort timer (`bffConfig().timeoutMs`, DEC-006, >= 240s) -> the BFF
//     returns 502 with `{"detail":"upstream timed out"}`. The request MAY already have been
//     received and be mid-processing on the backend -- retrying a non-idempotent call here
//     could duplicate its side effect (e.g. answering/charging a chat question twice).
//   - any other 502/503/504 that is NOT the BFF's own two messages above is a pass-through of
//     whatever status the backend itself returned (e.g. its own startup gate) -- same
//     "might already have been processed" uncertainty as a timeout.
// A real 4xx (401/403/404/422/429/...) is never any of the above and must never be treated as
// "waking up".
//
// Idempotency policy (the "explain your choice" the block's prompt asks for):
//   - "safe" (provably never reached the backend) -> ALWAYS retried automatically, with
//     backoff, up to a bounded total budget, whatever the HTTP method.
//   - "unsafe" (might have reached the backend) -> retried automatically ONLY when the caller
//     declares the call `idempotent` (every GET by default -- see api.ts's `request()` --
//     plus login, explicitly, because repeating it has no harmful side effect). For anything
//     else (chat, register, admin mutations, forms) a single "unsafe" failure is surfaced
//     immediately as a `WakeUpError` with NO automatic resend -- the caller shows the
//     "waking up" state with an explicit Retry button, so a duplicate is at most one real
//     extra human click, never a silent automatic one.
//
// Plain TypeScript, no DOM/Next.js imports, so `node --test` exercises the real logic.

export type WakeClass = "none" | "safe" | "unsafe";

/**
 * Classify a failed BFF response. `status === null` means the client's own `fetch()` to the
 * BFF route itself rejected (no HTTP response at all) -- the BFF route handler never even
 * started, so nothing reached the backend either: always "safe".
 */
export function classifyWakeSignal(status: number | null, detail: string | null): WakeClass {
  if (status === null) return "safe";
  if (status !== 502 && status !== 503 && status !== 504) return "none";
  if (status === 502 && detail === "upstream unreachable") return "safe";
  return "unsafe";
}

// Exponential backoff, base 1s, factor 1.8, capped at 8s per step, +/-20% jitter (injectable
// `rand` for deterministic tests). Attempt 1 is the first RETRY (after the initial attempt).
const BASE_DELAY_MS = 1_000;
const MAX_DELAY_MS = 8_000;
const BACKOFF_FACTOR = 1.8;

export function backoffDelayMs(attempt: number, rand: () => number = Math.random): number {
  const raw = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * Math.pow(BACKOFF_FACTOR, attempt - 1));
  const jitter = 1 + (rand() * 2 - 1) * 0.2; // 0.8x - 1.2x
  return Math.max(0, Math.round(raw * jitter));
}

// Total wall-clock budget before giving up and showing a real error + Retry button. 60s
// comfortably covers the backend's own cold start (seconds); a fully cold Ollama can take
// longer (P11.6b: embed wait up to 170s) -- callers on the chat path may pass a larger
// `budgetMs` (see api.ts's `CHAT_WAKE_BUDGET_MS`).
export const DEFAULT_WAKE_BUDGET_MS = 60_000;

export class WakeUpError extends Error {
  // "exhausted": retried in the "safe" (or safe-equivalent idempotent) loop until the budget
  // ran out. "unsafe": a single non-retryable failure for a non-idempotent call -- the caller
  // must show a manual Retry, never resend automatically.
  readonly phase: "exhausted" | "unsafe";

  constructor(phase: "exhausted" | "unsafe") {
    super(
      phase === "unsafe"
        ? "The server is waking up. Please retry."
        : "The server is taking longer than usual to wake up. Please retry.",
    );
    this.name = "WakeUpError";
    this.phase = phase;
  }
}

export interface WakeRetryOptions<T> {
  /** Performs one attempt; attempt number (0-based) is passed for logging/diagnostics only. */
  attempt: (attemptNumber: number) => Promise<T>;
  /** Maps a thrown error to a wake classification ("none" = rethrow it unchanged). */
  classifyError: (err: unknown) => WakeClass;
  /** True when retrying the SAME call again, even after an "unsafe" signal, is harmless. */
  idempotent: boolean;
  /** Called once, the first time the loop enters the waking state. */
  onWaking?: () => void;
  /** Called when an attempt finally succeeds after at least one waking retry. */
  onRecovered?: () => void;
  budgetMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  rand?: () => number;
}

const realSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Runs `attempt`, retrying cold-start-shaped failures per the policy documented above. */
export async function withWakeRetry<T>(opts: WakeRetryOptions<T>): Promise<T> {
  const budgetMs = opts.budgetMs ?? DEFAULT_WAKE_BUDGET_MS;
  const now = opts.now ?? Date.now;
  const sleep = opts.sleep ?? realSleep;
  const start = now();
  let waking = false;
  let attemptNumber = 0;

  for (;;) {
    try {
      const value = await opts.attempt(attemptNumber);
      if (waking) opts.onRecovered?.();
      return value;
    } catch (err) {
      const cls = opts.classifyError(err);
      if (cls === "none") throw err;

      if (cls === "unsafe" && !opts.idempotent) {
        // A single soft failure: show the waking state, but never resend automatically.
        opts.onWaking?.();
        throw new WakeUpError("unsafe");
      }

      // "safe", or "unsafe" treated as safe because the caller marked the call idempotent.
      if (!waking) {
        waking = true;
        opts.onWaking?.();
      }
      attemptNumber += 1;
      const elapsed = now() - start;
      if (elapsed >= budgetMs) throw new WakeUpError("exhausted");
      const delay = Math.min(backoffDelayMs(attemptNumber, opts.rand), budgetMs - elapsed);
      await sleep(delay);
    }
  }
}
