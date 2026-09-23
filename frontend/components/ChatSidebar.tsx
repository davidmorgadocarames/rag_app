"use client";

import { useState } from "react";

import type { ConversationSummary } from "@/lib/api";

interface ChatSidebarProps {
  conversations: ConversationSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

export default function ChatSidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
  onRename,
}: ChatSidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingDelete, setPendingDelete] = useState<ConversationSummary | null>(null);

  function startEdit(c: ConversationSummary) {
    setEditingId(c.id);
    setDraft(c.title);
  }

  function commitEdit() {
    if (editingId && draft.trim()) onRename(editingId, draft.trim());
    setEditingId(null);
  }

  return (
    <>
    <aside className="flex h-full w-full flex-col gap-3 border-r border-vault-steel-dark p-3">
      <button
        type="button"
        onClick={onNew}
        className="cursor-pointer border border-vault-amber px-3 py-2 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright"
      >
        + NEW CHAT
      </button>

      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
        {conversations.length === 0 && (
          <p className="px-1 py-2 text-xs text-vault-steel-dark">No conversations yet.</p>
        )}
        {conversations.map((c) => {
          const active = c.id === activeId;
          return (
            <div
              key={c.id}
              className={`group flex items-center gap-1 border px-2 py-2 text-sm ${
                active
                  ? "border-vault-amber text-vault-steel-light"
                  : "border-transparent text-vault-steel hover:border-vault-steel-dark"
              }`}
            >
              {editingId === c.id ? (
                <input
                  autoFocus
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={commitEdit}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitEdit();
                    if (e.key === "Escape") setEditingId(null);
                  }}
                  className="w-full border-b border-vault-amber bg-transparent text-sm text-vault-steel-light outline-none"
                />
              ) : (
                <button
                  type="button"
                  onClick={() => onSelect(c.id)}
                  className="min-w-0 flex-1 cursor-pointer truncate text-left"
                  title={c.title}
                >
                  {c.title}
                  <span className="ml-1 text-[10px] text-vault-steel-dark">
                    {c.total_tokens > 0 ? `· ${c.total_tokens} tok` : ""}
                  </span>
                </button>
              )}
              <button
                type="button"
                onClick={() => startEdit(c)}
                aria-label="Rename"
                className="cursor-pointer px-1 text-vault-steel-dark opacity-0 transition-opacity hover:text-vault-steel-light group-hover:opacity-100"
              >
                ✎
              </button>
              <button
                type="button"
                onClick={() => setPendingDelete(c)}
                aria-label="Delete"
                className="cursor-pointer px-1 text-vault-steel-dark opacity-0 transition-opacity hover:text-vault-danger group-hover:opacity-100"
              >
                ✕
              </button>
            </div>
          );
        })}
      </div>
    </aside>

      {pendingDelete && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/70 px-4"
          onClick={() => setPendingDelete(null)}
        >
          <div
            className="w-[min(360px,90vw)] border border-vault-steel-dark bg-vault-bg p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="text-sm text-vault-steel-light">Delete this conversation?</p>
            <p className="mt-1 truncate text-xs text-vault-steel-dark" title={pendingDelete.title}>
              {pendingDelete.title}
            </p>
            <p className="mt-2 text-xs text-vault-steel">This can&apos;t be undone.</p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingDelete(null)}
                className="cursor-pointer border border-vault-steel-dark px-4 py-2 text-xs tracking-[0.1em] text-vault-steel transition-colors hover:text-vault-steel-light"
              >
                CANCEL
              </button>
              <button
                type="button"
                onClick={() => {
                  onDelete(pendingDelete.id);
                  setPendingDelete(null);
                }}
                className="cursor-pointer border border-vault-danger px-4 py-2 text-xs tracking-[0.1em] text-vault-danger transition-colors hover:bg-vault-danger hover:text-vault-bg"
              >
                DELETE
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
