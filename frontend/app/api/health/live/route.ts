// /api/health/live (T16.2.1): the k8s livenessProbe -- process only, NEVER calls the
// backend. /api/health (the existing route) already proxies the backend's own /health and
// is the readinessProbe's target (it IS the "deps reachable" check); this route must answer
// 200 as long as the Next.js server itself is alive, so a transient backend outage never
// restarts the frontend container.
export async function GET(): Promise<Response> {
  return Response.json({ status: "ok" });
}
