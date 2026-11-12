"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import VaultDoor from "@/components/VaultDoor";
import { login, register, resendVerification } from "@/lib/api";

export default function LoginPanel() {
  const router = useRouter();
  const [opened, setOpened] = useState(false);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // T12.4.4: register no longer opens a session -- its generic response is shown here
  // instead, and the panel switches back to the login form.
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // T12.4.4: with ENV=prod (REQUIRE_EMAIL_VERIFICATION defaults on), an unverified account
  // cannot log in at all -- and has no session, so /account's own "Resend verification"
  // button is unreachable too. Offering it right here, on the exact failed-login error, is
  // the only UI path left to request a fresh email without one.
  const [showResend, setShowResend] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    setShowResend(false);
    try {
      if (mode === "login") {
        await login(email, password);
        // The session is a cookie the BFF/backend just set (T12.1.3/T12.1.7) -- nothing to
        // store client-side; /chat's own guard (middleware + a real API call) takes it from
        // here.
        router.push("/chat");
        return;
      }
      const res = await register(email, password);
      setMessage(res.detail);
      setMode("login");
      setPassword("");
    } catch (err) {
      const detail = err instanceof Error ? err.message : "Something went wrong";
      setError(detail);
      if (mode === "login" && detail === "email not verified") setShowResend(true);
    } finally {
      setBusy(false);
    }
  }

  async function onResend() {
    setMessage(null);
    try {
      const res = await resendVerification(email);
      setMessage(res.detail);
      setShowResend(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    }
  }

  return (
    <>
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
            minLength={12}
            placeholder="password (min 12 characters)"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="border-0 border-b border-vault-steel-dark bg-transparent px-0.5 py-2 text-vault-steel-light outline-none placeholder:text-vault-steel-dark focus:border-vault-amber"
          />
          {message && <p className="text-xs text-vault-steel">{message}</p>}
          {error && <p className="text-xs text-vault-danger">{error}</p>}
          {showResend && (
            <button
              type="button"
              onClick={onResend}
              className="w-fit cursor-pointer text-xs tracking-[0.1em] text-vault-steel-dark transition-colors hover:text-vault-steel"
            >
              Resend verification email
            </button>
          )}
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
          {mode === "login" && (
            // T12.4.6: the forgot-password request form lives on its own page.
            <a
              href="/forgot-password"
              className="text-xs tracking-[0.1em] text-vault-steel-dark transition-colors hover:text-vault-steel"
            >
              Forgot password?
            </a>
          )}
        </form>
      </div>
    </>
  );
}
