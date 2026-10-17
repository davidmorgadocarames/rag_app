import type { Metadata } from "next";
import "./globals.css";

// D12.2 (T12.2.2/T12.2.3): every page under this layout renders dynamically (SSR), never
// statically pre-rendered -- middleware.ts generates a FRESH nonce per request and the CSP
// header carries it; a statically-generated page would bake in one build-time nonce and
// serve it to every visitor forever, which is not a nonce at all. Segment config is
// inherited from the root layout unless a page overrides it, so this one line covers the
// whole app.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "SecRAG — OWASP security assistant",
  description:
    "Agentic RAG assistant answering OWASP security questions from a versioned corpus.",
  // The UI ships a deliberate dark palette; tell Dark Reader to leave it alone so it
  // doesn't re-tint the vault theme or inject attributes that break hydration.
  other: { "darkreader-lock": "1" },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // suppressHydrationWarning: browser extensions (e.g. Dark Reader) mutate <html>/<body>
  // before React hydrates; ignore those attribute diffs on these elements only.
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        suppressHydrationWarning
        className="min-h-screen bg-vault-bg text-vault-steel-light antialiased"
      >
        {children}
      </body>
    </html>
  );
}
