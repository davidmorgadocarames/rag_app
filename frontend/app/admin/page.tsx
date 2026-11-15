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
import AdminDashboard from "@/components/admin/AdminDashboard";

export const dynamic = "force-dynamic"; // never statically generated (D12.2) -- a fresh check every time

async function loadFeatures(): Promise<AdminFeaturesResponse | null> {
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
    if (!response.ok) return null; // not an admin, no MFA step-up, or the backend is down
    return (await response.json()) as AdminFeaturesResponse;
  } catch {
    return null; // backend unreachable: fail closed, never render admin content
  }
}

export default async function AdminPage() {
  const initial = await loadFeatures();

  if (initial === null) {
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

  return <AdminDashboard initial={initial} />;
}
