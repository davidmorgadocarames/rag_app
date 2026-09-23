"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import VaultDoor from "@/components/VaultDoor";
import { login, register } from "@/lib/api";
import { setToken } from "@/lib/session";

export default function LoginPage() {
  const router = useRouter();
  const [opened, setOpened] = useState(false);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const action = mode === "login" ? login : register;
      const { access_token } = await action(email, password);
      setToken(access_token);
      router.push("/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center bg-vault-bg px-4">
      {/* Unmount the door the moment it finishes opening, so its zoomed sprite
          doesn't linger behind the access panel while it fades in. */}
      {!opened && <VaultDoor mode="interactive" onOpened={() => setOpened(true)} />}

      {/* Access panel — revealed once the door is open. */}
      <div
        className={`fixed inset-0 flex items-center justify-center bg-vault-bg px-4 transition-opacity duration-700 ${
          opened ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <form onSubmit={onSubmit} className="flex w-[min(320px,84vw)] flex-col gap-4">
          <h1 className="mb-1 text-sm font-normal tracking-[0.28em] text-vault-amber">
            {mode === "login" ? "IDENTIFY YOURSELF" : "CREATE ACCESS"}
          </h1>
          <input
            type="email"
            required
            placeholder="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="border-0 border-b border-vault-steel-dark bg-transparent px-0.5 py-2 text-vault-steel-light outline-none placeholder:text-vault-steel-dark focus:border-vault-amber"
          />
          <input
            type="password"
            required
            minLength={8}
            placeholder="password (min 8 characters)"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="border-0 border-b border-vault-steel-dark bg-transparent px-0.5 py-2 text-vault-steel-light outline-none placeholder:text-vault-steel-dark focus:border-vault-amber"
          />
          {error && <p className="text-xs text-vault-danger">{error}</p>}
          <button
            type="submit"
            disabled={busy}
            className="mt-2 cursor-pointer border border-vault-amber bg-transparent px-4 py-2.5 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright disabled:opacity-40"
          >
            {busy ? "…" : mode === "login" ? "ENTER" : "REGISTER"}
          </button>
          <button
            type="button"
            onClick={() => setMode(mode === "login" ? "register" : "login")}
            className="text-xs tracking-[0.1em] text-vault-steel-dark transition-colors hover:text-vault-steel"
          >
            {mode === "login" ? "Need access? Register" : "Have access? Sign in"}
          </button>
        </form>
      </div>
    </main>
  );
}
