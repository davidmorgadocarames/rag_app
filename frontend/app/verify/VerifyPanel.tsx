"use client";

// T12.4.5: a GET of this page (the email link itself) only ever renders; THIS button click
// is what actually POSTs the token to the backend (through the BFF) -- a mail scanner
// pre-fetching the GET link can never consume it.
import { useState } from "react";

import { ApiError, verifyEmail } from "@/lib/api";

export default function VerifyPanel({ token }: { token: string }) {
  const [status, setStatus] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [message, setMessage] = useState<string | null>(null);

  async function onVerify() {
    setStatus("busy");
    setMessage(null);
    try {
      const res = await verifyEmail(token);
      setMessage(res.detail);
      setStatus("done");
    } catch (err) {
      setMessage(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Something went wrong",
      );
      setStatus("error");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {!token ? (
        <p className="text-sm text-vault-danger" role="alert">
          This link is missing its verification token.
        </p>
      ) : (
        status !== "done" && (
          <button
            type="button"
            onClick={onVerify}
            disabled={status === "busy"}
            className="w-fit cursor-pointer border border-vault-amber bg-transparent px-4 py-2.5 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright disabled:opacity-40"
          >
            {status === "busy" ? "…" : "VERIFY EMAIL"}
          </button>
        )
      )}
      {message && (
        <p className="text-sm text-vault-steel-light" role="status">
          {message}
        </p>
      )}
      {status === "done" && (
        <a
          href="/login"
          className="w-fit text-xs tracking-[0.1em] text-vault-steel-dark transition-colors hover:text-vault-steel"
        >
          Go to sign in
        </a>
      )}
    </div>
  );
}
