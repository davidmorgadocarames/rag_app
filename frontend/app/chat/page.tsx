"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import ChatSidebar from "@/components/ChatSidebar";
import {
  type Citation,
  type ConversationSummary,
  deleteConversation as apiDeleteConversation,
  getConversation,
  listConversations,
  renameConversation as apiRenameConversation,
  streamChat,
} from "@/lib/api";
import { getToken } from "@/lib/session";

interface UiMessage {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  abstained?: boolean;
  grounded?: boolean;
  tokens?: number;
  streaming?: boolean;
}

const STAGE_LABELS: Record<string, string> = {
  classifying: "Classifying…",
  responding: "Responding…",
  retrieving: "Retrieving sources…",
  reranking: "Reranking…",
  generating: "Generating…",
  checking: "Checking grounding…",
};

function Citations({ citations }: { citations: Citation[] }) {
  if (!citations || citations.length === 0) return null;
  return (
    <div className="mt-3 flex flex-col gap-1">
      <p className="text-xs uppercase tracking-[0.18em] text-vault-steel">Sources</p>
      {citations.map((c) => (
        <span
          key={c.marker}
          className="w-fit border border-vault-steel-dark bg-vault-plate px-2 py-1 text-xs text-vault-steel"
        >
          [{c.marker}] {c.heading} · {c.version}
          {c.effective_date ? ` · ${c.effective_date}` : ""}
        </span>
      ))}
    </div>
  );
}

export default function ChatPage() {
  const router = useRouter();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [convTotal, setConvTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);

  const refreshConversations = useCallback(async () => {
    const token = getToken();
    if (!token) return;
    try {
      setConversations(await listConversations(token));
    } catch {
      // ignore list errors; the thread still works
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void refreshConversations();
  }, [router, refreshConversations]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, stage]);

  async function selectConversation(id: string) {
    const token = getToken();
    if (!token) return;
    setSidebarOpen(false);
    try {
      const detail = await getConversation(id, token);
      setActiveId(id);
      setMessages(
        detail.messages.map((m) => ({
          role: m.role,
          content: m.content,
          citations: m.citations,
          abstained: m.abstained,
          grounded: m.grounded,
          tokens: (m.prompt_tokens ?? 0) + (m.completion_tokens ?? 0) || undefined,
        })),
      );
      setConvTotal(
        detail.messages.reduce(
          (sum, m) => sum + (m.prompt_tokens ?? 0) + (m.completion_tokens ?? 0),
          0,
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load conversation");
    }
  }

  function newChat() {
    setActiveId(null);
    setMessages([]);
    setConvTotal(0);
    setError(null);
    setSidebarOpen(false);
  }

  async function onDelete(id: string) {
    const token = getToken();
    if (!token) return;
    await apiDeleteConversation(id, token);
    if (id === activeId) newChat();
    void refreshConversations();
  }

  async function onRename(id: string, title: string) {
    const token = getToken();
    if (!token) return;
    await apiRenameConversation(id, title, token);
    void refreshConversations();
  }

  function patchLastAssistant(patch: Partial<UiMessage>) {
    setMessages((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].role === "assistant") {
          next[i] = { ...next[i], ...patch };
          break;
        }
      }
      return next;
    });
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = getToken();
    const question = input.trim();
    if (!question || busy || !token) return;

    setInput("");
    setError(null);
    setBusy(true);
    setStage("classifying");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: question },
      { role: "assistant", content: "", streaming: true },
    ]);

    await streamChat(
      { question, conversation_id: activeId ?? undefined },
      token,
      {
        onStage: (s) => setStage(s),
        onToken: (t) => appendToken(t),
        onDone: (d) => {
          patchLastAssistant({
            content: d.answer,
            citations: d.citations,
            abstained: d.abstained,
            grounded: d.grounded,
            tokens: d.usage.total_tokens,
            streaming: false,
          });
          setConvTotal(d.conversation_total_tokens);
          if (!activeId) setActiveId(d.conversation_id);
          void refreshConversations();
          setBusy(false);
          setStage(null);
        },
        onError: (detail) => {
          patchLastAssistant({ content: `⚠ ${detail}`, streaming: false });
          setBusy(false);
          setStage(null);
        },
      },
    );
  }

  function appendToken(t: string) {
    setMessages((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].role === "assistant") {
          next[i] = { ...next[i], content: next[i].content + t };
          break;
        }
      }
      return next;
    });
  }

  return (
    <main className="flex h-screen bg-vault-bg">
      <div
        className={`${sidebarOpen ? "block" : "hidden"} absolute inset-y-0 left-0 z-10 w-64 bg-vault-bg md:static md:block`}
      >
        <ChatSidebar
          conversations={conversations}
          activeId={activeId}
          onSelect={selectConversation}
          onNew={newChat}
          onDelete={onDelete}
          onRename={onRename}
        />
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-vault-steel-dark px-4 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setSidebarOpen((v) => !v)}
              className="cursor-pointer text-vault-steel md:hidden"
              aria-label="Toggle conversations"
            >
              ☰
            </button>
            <Link href="/" className="text-lg font-bold tracking-[0.12em] text-vault-steel-light">
              SecRAG
            </Link>
          </div>
          <div className="flex items-center gap-4">
            <span className="text-xs text-vault-steel-dark">{convTotal} tokens</span>
            <Link
              href="/account"
              className="text-sm text-vault-steel transition-colors hover:text-vault-steel-light"
            >
              Account
            </Link>
          </div>
        </header>

        <div ref={threadRef} className="flex-1 overflow-y-auto px-4 py-6">
          <div className="mx-auto flex max-w-2xl flex-col gap-5">
            {messages.length === 0 && (
              <p className="mt-10 text-center text-sm text-vault-steel-dark">
                Ask a security question — e.g. &ldquo;How do I prevent SQL injection?&rdquo;
              </p>
            )}
            {messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="self-end border border-vault-steel-dark px-3 py-2 text-vault-steel-light">
                  {m.content}
                </div>
              ) : m.abstained ? (
                <div key={i} className="border border-vault-amber bg-vault-plate/40 p-4 text-vault-amber">
                  <p className="font-medium tracking-[0.06em]">No reliable source in the corpus</p>
                  <p className="mt-1 text-sm text-vault-steel-light">{m.content}</p>
                </div>
              ) : (
                <div key={i} className="border border-vault-steel-dark bg-vault-plate/40 p-4">
                  <p className="whitespace-pre-wrap text-vault-steel-light">
                    {m.content}
                    {m.streaming && <span className="animate-pulse text-vault-amber"> ▍</span>}
                  </p>
                  {!m.streaming && <Citations citations={m.citations ?? []} />}
                  {m.tokens ? (
                    <p className="mt-2 text-[10px] text-vault-steel-dark">{m.tokens} tokens</p>
                  ) : null}
                </div>
              ),
            )}
            {busy && stage && (
              <p className="text-xs tracking-[0.1em] text-vault-amber">{STAGE_LABELS[stage] ?? stage}</p>
            )}
            {error && <p className="text-sm text-vault-danger">{error}</p>}
          </div>
        </div>

        <form onSubmit={onSubmit} className="border-t border-vault-steel-dark px-4 py-3">
          <div className="mx-auto flex max-w-2xl items-end gap-3">
            <textarea
              rows={2}
              placeholder="Ask a security question…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
              className="flex-1 resize-none border border-vault-steel-dark bg-transparent px-3 py-2 text-vault-steel-light outline-none placeholder:text-vault-steel-dark focus:border-vault-amber"
            />
            <button
              type="submit"
              disabled={busy}
              className="cursor-pointer border border-vault-amber px-6 py-2.5 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright disabled:opacity-40"
            >
              {busy ? "…" : "SEND"}
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}
