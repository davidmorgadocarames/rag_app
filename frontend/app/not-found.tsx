// Global 404 (T13.10.1/T13.10.2, DEBT-057): Next's App Router renders this automatically,
// with a real HTTP 404 status, for any request that matches no route at all -- no extra
// wiring needed, and no soft-404 200 to avoid. `/api/*` has its own catch-all route
// (app/api/[...path]/route.ts) that always answers in JSON, so it is never reached here;
// `/admin` for a non-admin is a real route match (app/admin/page.tsx does its own
// server-side role check) and is likewise never turned into this page.
//
// Deliberately NEVER reads the pathname (no usePathname()/useSearchParams()/headers()) --
// see lib/notFoundContent.ts's own comment: there is nothing here that could echo the
// requested path back to the visitor, and exactly one way out: "Back to login".
import Link from "next/link";

import {
  BACK_TO_LOGIN_HREF,
  BACK_TO_LOGIN_LABEL,
  NOT_FOUND_BODY,
  NOT_FOUND_CODE,
  NOT_FOUND_HEADING,
  NOT_FOUND_LABEL,
} from "@/lib/notFoundContent";
import NotFoundVault from "@/components/NotFoundVault";

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col items-center justify-center gap-6 px-4 py-10">
      <NotFoundVault />

      <section
        aria-labelledby="not-found-heading"
        className="flex max-w-[60ch] flex-col items-center gap-4 text-center"
      >
        <div className="flex flex-col items-center gap-1.5">
          <b className="text-[clamp(56px,12vw,96px)] leading-none tracking-[0.12em] text-vault-amber">
            {NOT_FOUND_CODE}
          </b>
          <span className="text-[clamp(13px,2vw,16px)] uppercase tracking-[0.32em] text-vault-steel">
            {NOT_FOUND_LABEL}
          </span>
        </div>

        <h1
          id="not-found-heading"
          className="text-balance text-[clamp(20px,3.6vw,28px)] tracking-[0.08em] text-vault-steel-light"
        >
          {NOT_FOUND_HEADING}
        </h1>

        <p className="text-sm leading-relaxed text-vault-steel">{NOT_FOUND_BODY}</p>

        <Link
          href={BACK_TO_LOGIN_HREF}
          className="border border-vault-amber px-6 py-2.5 text-xs uppercase tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-vault-amber"
        >
          {BACK_TO_LOGIN_LABEL}
        </Link>
      </section>
    </main>
  );
}
