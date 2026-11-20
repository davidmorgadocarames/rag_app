// Admin dashboard shell (T13.2.1): a server-side role + MFA check BEFORE anything
// admin-shaped is ever rendered -- never a client-side decision (T12.3.6 case 5). Reuses the
// exact same BFF proxy logic (`lib/bffProxy.ts`) every other route uses, so this check is
// not a second, possibly-drifting implementation of "is this user an admin" -- and doubles
// as the dashboard's own "first data load" (T13.2 rendering rule: SSR shell + server-side
// role/MFA check + first data load; everything live after that is CSR, see
// components/admin/AdminDashboard.tsx).

import { headers } from "next/headers";

import type { AdminFeaturesResponse } from "@/lib/api";
import { bffConfig } from "@/lib/bffConfig";
import { proxyToBackend } from "@/lib/bffProxy";
import { classifyWakeSignal } from "@/lib/wakeUp";
import AdminDashboard from "@/components/admin/AdminDashboard";
import WakeBanner from "@/components/WakeBanner";

export const dynamic = "force-dynamic"; // never statically generated (D12.2) -- a fresh check every time

type LoadResult =
  | { ok: true; data: AdminFeaturesResponse }
  // T13.9.1 (DEBT-056): distinguishes "really not an admin" (a real 4xx: show today's
  // message) from a cold-start-shaped failure (never show it as "you do not have access" --
  // that would be a 4xx-shaped lie about a user who may well be an admin).
  | { ok: false; waking: boolean };

async function loadFeatures(): Promise<LoadResult> {
  // next/headers' ReadonlyHeaders only differs from DOM Headers in being read-only --
  // proxyToBackend only ever calls .get() on it, so this cast is structurally safe.
  const incoming = (await headers()) as unknown as Headers;
  try {
    const response = await proxyToBackend(
      {
        method: "GET",
        path: "/admin/features",
        search: "",
        headers: incoming,
        body: null,
      },
      bffConfig(),
    );
    if (response.ok) return { ok: true, data: (await response.json()) as AdminFeaturesResponse };
    let detail: string | null = null;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // keep detail null
    }
    return { ok: false, waking: classifyWakeSignal(response.status, detail) !== "none" };
  } catch {
    // The BFF's own fetch to the backend never got a response at all -- the same signature
    // as a cold start with no replica listening yet.
    return { ok: false, waking: true };
  }
}

export default async function AdminPage() {
  const result = await loadFeatures();

  if (!result.ok && result.waking) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">Admin</h1>
        <WakeBanner state="waking" />
        <p className="text-sm text-vault-steel">
          <a href="/admin" className="underline">
            Reload this page
          </a>{" "}
          in a little while.
        </p>
      </main>
    );
  }

  if (!result.ok) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">Admin</h1>
        <p className="text-sm text-vault-steel">
          You do not have access to this page. Admin routes require the admin role and a
          fresh two-factor step-up (see{" "}
          <a href="/account/security" className="underline">
            Account security
          </a>
          ).
        </p>
      </main>
    );
  }

  return <AdminDashboard initial={result.data} />;
}
