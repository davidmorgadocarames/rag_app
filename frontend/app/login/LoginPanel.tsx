"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import VaultDoor from "@/components/VaultDoor";
import WakeBanner from "@/components/WakeBanner";
import { login, register, resendVerification } from "@/lib/api";
import { createRetryGuard } from "@/lib/retryGuard";
import { useWakeState } from "@/lib/useWakeState";
import { WakeUpError } from "@/lib/wakeUp";
import { sendWakePing } from "@/lib/wakePing";

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
  // T13.9.1 (DEBT-056): login retries a cold-start-shaped failure automatically (wake.state
  // "waking"); if that gives up, wake.state becomes "failed" and the user presses Retry
  // below -- re-running the exact same submit, with email/password still in state (never
  // cleared on error), so nothing typed is lost.
  const wake = useWakeState();
  // DA-13-H-1 fix (13-H-fix): the Retry button below calls `submitLogin()` directly with no
  // guard of its own -- `busy` is already false again by the time the Retry banner renders,
  // so two clicks in the same tick would both pass the (unrelated) submit-button disable and
  // fire a second `login()` call. `retryGuardRef` blocks that synchronously; `retrying` only
  // drives the button's `disabled` look.
  const retryGuardRef = useRef(createRetryGuard());
  const [retrying, setRetrying] = useState(false);

  // T13.13.2 (DEBT-061): one light wake ping per page load -- nudges Ollama/bge-m3 awake in
  // parallel with whatever else is cold-starting, well before the visitor even submits the
  // login form. Fire-and-forget (see lib/wakePing.ts); an empty dependency array means this
  // runs exactly once per mount, not on every re-render.
  useEffect(() => {
    sendWakePing();
  }, []);

  async function submitLogin() {
    setBusy(true);
    setError(null);
    setMessage(null);
    setShowResend(false);
    try {
      if (mode === "login") {
        // T13.11.5 (DEBT-060): purely visual -- shows the SAME banner if this call is still
        // pending ~3s later, even when the Container Apps activator just HOLDS the request
        // (no error, no retry signal at all) during a scale-from-zero. clearPending() runs
        // whatever the outcome, so a fast login never shows it at all.
        wake.startPending();
        try {
          await login(email, password, { onWaking: wake.onWaking, onRecovered: wake.onRecovered });
        } finally {
          wake.clearPending();
        }
        wake.reset();
        // The session is a cookie the BFF/backend just set (T12.1.3/T12.1.7) -- nothing to
        // store client-side; /chat's own guard (middleware + a real API call) takes it from
        // here.
        router.push("/chat");
        return;
      }
      const res = await register(email, password);
      wake.reset();
      setMessage(res.detail);
      setMode("login");
      setPassword("");
    } catch (err) {
      if (err instanceof WakeUpError) {
        wake.onFailed();
        return;
      }
      const detail = err instanceof Error ? err.message : "Something went wrong";
      setError(detail);
      if (mode === "login" && detail === "email not verified") setShowResend(true);
    } finally {
      setBusy(false);
    }
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await submitLogin();
  }

  async function onRetryLogin() {
    await retryGuardRef.current.run(async () => {
      setRetrying(true);
      try {
        await submitLogin();
      } finally {
        setRetrying(false);
      }
    });
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
          {error && <p className="text-xs text-vault-danger-text">{error}</p>}
          <WakeBanner state={wake.state} onRetry={() => void onRetryLogin()} retrying={retrying} />
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
