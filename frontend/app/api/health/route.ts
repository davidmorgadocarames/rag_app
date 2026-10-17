// /api/health (DEC-005, allow-listed in bffRoutes.ts's spirit, but handled separately since
// it never proxies the backend's response body -- no body details ever cross the BFF here).
import { bffConfig } from "@/lib/bffConfig";

export async function GET(): Promise<Response> {
  const { backendUrl, timeoutMs } = bffConfig();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const upstream = await fetch(`${backendUrl}/health`, { signal: controller.signal });
    return Response.json({ status: upstream.ok ? "ok" : "error" }, { status: upstream.ok ? 200 : 503 });
  } catch {
    return Response.json({ status: "error" }, { status: 503 });
  } finally {
    clearTimeout(timer);
  }
}
