import Link from "next/link";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-6 px-6">
      <span className="inline-block w-fit rounded-full border border-slate-300 px-3 py-1 text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
        OWASP security assistant
      </span>
      <h1 className="text-4xl font-bold tracking-tight">SecRAG</h1>
      <p className="text-lg text-slate-600 dark:text-slate-300">
        An agentic RAG assistant for OWASP security guidance — version-aware,
        cites its sources, and honest about what it doesn&apos;t know.
      </p>
      <div className="flex gap-3">
        <Link
          href="/chat"
          className="rounded-lg bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-500"
        >
          Open the assistant
        </Link>
        <Link
          href="/login"
          className="rounded-lg border border-slate-300 px-4 py-2 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          Sign in
        </Link>
      </div>
    </main>
  );
}
