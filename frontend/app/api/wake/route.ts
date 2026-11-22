// /api/wake (T13.13.2, DEBT-061): the wake ping /login and /chat fire once per page load
// (lib/wakePing.ts), forwarded to the backend's unauthenticated GET /wake. Handled the SAME
// way as /api/health (own file, bypassing the generic bffProxy/ALLOWED_ROUTES path) -- the
// backend's response is NEVER relayed, not even its status code: this route always answers
// 204, whether the warm-up was started, skipped (one already in flight) or the backend did
// not answer at all. A caller must learn nothing about Ollama's/the backend's internal
// state from this endpoint, only "the request was accepted".
import { bffConfig } from "@/lib/bffConfig";

export async function GET(): Promise<Response> {
  const { backendUrl, timeoutMs } = bffConfig();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    await fetch(`${backendUrl}/wake`, { signal: controller.signal });
  } catch {
    // best-effort: never surfaced to the caller (see the header above).
  } finally {
    clearTimeout(timer);
  }
  return new Response(null, { status: 204 });
}
