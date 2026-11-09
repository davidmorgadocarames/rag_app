// Admin placeholder page (T12.3.2/T12.3.5): a server-side role + MFA check BEFORE anything
// admin-shaped is ever rendered -- never a client-side decision (T12.3.6 case 5). Reuses the
// exact same BFF proxy logic (`lib/bffProxy.ts`) the real `/api/admin/ping` route uses, so
// this check is not a second, possibly-drifting implementation of "is this user an admin".
//
// Written in 12b-A; NOT run through `npm run build`/Playwright this session (PC constraint)
// -- verification is phase 2 work.

import { headers } from "next/headers";

import { bffConfig } from "@/lib/bffConfig";
import { proxyToBackend } from "@/lib/bffProxy";

export const dynamic = "force-dynamic"; // never statically generated (D12.2) -- a fresh check every time

async function isSteppedUpAdmin(): Promise<boolean> {
  // next/headers' ReadonlyHeaders only differs from DOM Headers in being read-only --
  // proxyToBackend only ever calls .get() on it, so this cast is structurally safe.
  const incoming = (await headers()) as unknown as Headers;
  try {
    const response = await proxyToBackend(
      {
        method: "GET",
        path: "/admin/ping",
        search: "",
        headers: incoming,
        body: null,
      },
      bffConfig(),
    );
    return response.ok;
  } catch {
    return false; // backend unreachable: fail closed, never render admin content
  }
}

export default async function AdminPage() {
  const allowed = await isSteppedUpAdmin();

  if (!allowed) {
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

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-6">
      <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">Admin</h1>
      <p className="text-sm text-vault-steel">
        Placeholder (T12.3.2/T12.3.5) -- confirms the permission map + MFA step-up gate a real
        page end to end. Real admin features arrive in Phase 13.
      </p>
    </main>
  );
}
