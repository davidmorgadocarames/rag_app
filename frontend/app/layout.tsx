import type { Metadata } from "next";
import "./globals.css";

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
