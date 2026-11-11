"use client";

// T12.4.5 (frontend shell) / T12.4.6 (12b-C, the backend route): a GET of this page (the
// email link) only renders the form; this submit is what POSTs the token + new password.
// The backend route this calls lands in 12b-C -- until then the BFF's own allow-list 404s
// it (lib/bffRoutes.ts), surfaced below as an ordinary error, same as any other API error.
import { useState } from "react";

import { confirmPasswordReset } from "@/lib/api";

export default function ResetPanel({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [message, setMessage] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus("busy");
    setMessage(null);
    try {
      const res = await confirmPasswordReset(token, password);
      setMessage(res.detail);
      setStatus("done");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Something went wrong");
      setStatus("error");
    }
  }

  if (!token) {
    return (
      <p className="text-sm text-vault-danger" role="alert">
        This link is missing its reset token.
      </p>
    );
  }

  if (status === "done") {
    return (
      <div className="flex flex-col gap-4">
        <p className="text-sm text-vault-steel-light" role="status">
          {message}
        </p>
        <a
          href="/login"
          className="w-fit text-xs tracking-[0.1em] text-vault-steel-dark transition-colors hover:text-vault-steel"
        >
          Go to sign in
        </a>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      <input
        type="password"
        required
        minLength={12}
        placeholder="new password (min 12 characters)"
        autoComplete="new-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className="border-0 border-b border-vault-steel-dark bg-transparent px-0.5 py-2 text-vault-steel-light outline-none placeholder:text-vault-steel-dark focus:border-vault-amber"
      />
      {message && (
        <p className="text-sm text-vault-danger" role="alert">
          {message}
        </p>
      )}
      <button
        type="submit"
        disabled={status === "busy"}
        className="w-fit cursor-pointer border border-vault-amber bg-transparent px-4 py-2.5 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright disabled:opacity-40"
      >
        {status === "busy" ? "…" : "SET NEW PASSWORD"}
      </button>
    </form>
  );
}
