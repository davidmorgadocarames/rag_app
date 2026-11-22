// A single, best-effort "wake ping" fired once per page load from /login and /chat
// (T13.13.2, DEBT-061): a tiny GET to this app's own `/api/wake` BFF route, which forwards
// to the backend's unauthenticated `GET /wake` and triggers its background Ollama warm-up
// (`rag_app.warmup`) if Ollama happens to be cold -- in parallel with whatever else is
// cold-starting, instead of only starting once the visitor submits a question.
//
// Fire-and-forget BY DESIGN: the caller never awaits the network result and never learns
// whether it succeeded, timed out, or hit the server-side rate limit -- a slow/failed wake
// ping must never delay or otherwise affect the page in any visible way (no loading state,
// no retry, no error surfaced). `fetchImpl` is injectable so this is unit-testable without a
// real network call or a DOM.

export function sendWakePing(fetchImpl: typeof fetch = fetch): void {
  try {
    void fetchImpl("/api/wake", { method: "GET", cache: "no-store" }).catch(() => {
      // best-effort: a rejected promise (network error, abort, ...) must never surface.
    });
  } catch {
    // best-effort: a fetch implementation that throws synchronously must not either.
  }
}
