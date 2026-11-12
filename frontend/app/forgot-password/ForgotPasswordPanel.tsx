"use client";

// T12.4.6: the form that requests a reset email (the actual consume happens on /reset,
// linked from that email). Always shows the SAME generic message on success, whatever the
// outcome server-side (GENERIC_FORGOT_PASSWORD_MESSAGE, F3/F11 enumeration-safe) -- never a
// different message for "no such account".
import { useState } from "react";

import { forgotPassword } from "@/lib/api";

export default function ForgotPasswordPanel() {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [message, setMessage] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus("busy");
    setMessage(null);
    try {
      const res = await forgotPassword(email);
      setMessage(res.detail);
      setStatus("done");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Something went wrong");
      setStatus("error");
    }
  }

  if (status === "done") {
    return (
      <p className="text-sm text-vault-steel-light" role="status">
        {message}
      </p>
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      <input
        type="email"
        required
        placeholder="email"
        autoComplete="username"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
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
        {status === "busy" ? "…" : "SEND RESET LINK"}
      </button>
    </form>
  );
}
