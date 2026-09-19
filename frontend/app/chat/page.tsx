"use client";

import Link from "next/link";
import { useState } from "react";

import { chat, type ChatResponse } from "@/lib/api";
import { getToken } from "@/lib/session";

function Citations({ citations }: { citations: ChatResponse["citations"] }) {
  if (citations.length === 0) return null;
  return (
    <div className="mt-3 flex flex-col gap-1">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Sources</p>
      {citations.map((c) => (
        <span
          key={c.marker}
          className="w-fit rounded bg-slate-100 px-2 py-1 font-mono text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300"
        >
          [{c.marker}] {c.heading} · {c.version}
          {c.effective_date ? ` · ${c.effective_date}` : ""}
        </span>
      ))}
    </div>
  );
}

export default function ChatPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<ChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await chat(question, getToken() ?? undefined));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-6 px-6 py-8">
      <header className="flex items-center justify-between">
        <Link href="/" className="text-lg font-bold tracking-tight">
          SecRAG
        </Link>
        <Link href="/account" className="text-sm text-slate-500 hover:underline">
          Account
        </Link>
      </header>

      <form onSubmit={onSubmit} className="flex flex-col gap-3">
        <textarea
          rows={3}
          placeholder="Ask a security question, e.g. How do I prevent SQL injection?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          className="rounded-lg border border-slate-300 bg-transparent px-3 py-2 focus:border-blue-500 focus:outline-none dark:border-slate-700"
        />
        <button
          type="submit"
          disabled={busy}
          className="w-fit rounded-lg bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {busy ? "Thinking…" : "Send"}
        </button>
      </form>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {result &&
        (result.abstained ? (
          <div className="rounded-lg border border-amber-400 bg-amber-50 p-4 text-amber-800 dark:border-amber-600 dark:bg-amber-950/40 dark:text-amber-300">
            <p className="font-medium">No reliable source in the corpus</p>
            <p className="mt-1 text-sm">{result.answer}</p>
          </div>
        ) : (
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900">
            <p className="whitespace-pre-wrap">{result.answer}</p>
            <Citations citations={result.citations} />
          </div>
        ))}
    </main>
  );
}
