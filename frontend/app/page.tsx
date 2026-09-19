export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-6 px-6">
      <div>
        <span className="inline-block rounded-full border border-slate-300 px-3 py-1 text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
          Foundation · commit 1
        </span>
      </div>
      <h1 className="text-4xl font-bold tracking-tight">SecRAG</h1>
      <p className="text-lg text-slate-600 dark:text-slate-300">
        An agentic RAG assistant for OWASP security guidance — version-aware,
        honest about what it doesn&apos;t know, and built like a real
        application.
      </p>
      <p className="text-sm text-slate-500 dark:text-slate-400">
        The application is under construction. See the documentation in{" "}
        <code className="rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-800">
          /docs
        </code>{" "}
        for the product and technical design.
      </p>
    </main>
  );
}
