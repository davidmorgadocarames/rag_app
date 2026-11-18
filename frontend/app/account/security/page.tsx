"use client";

// Admin MFA enrolment + step-up (T12.3.5). CSR only (T12.1.9 "SSR shell + CSR form"
// convention, like /login): the real guard is `middleware.ts` (has-a-cookie, UX only) +
// this page's own 401 handling below, and ultimately the backend (`rag_app.authz`) on
// every call.
//
// QR rendering (DEC-024 = B, phase 2): `components/QrCode.tsx` renders the `otpauth://`
// provisioning URI as an inline SVG, client-side only, no network call -- the secret never
// leaves the browser (not logged, not placed in any URL/query). The raw secret and URI are
// kept visible underneath as a manual-entry fallback for any TOTP app that cannot scan.
//
// T13.7.4/DEBT-051 (DEC-041): a returning account that already has MFA enabled used to see
// the SAME "Start enrolment" button every new session -- re-enrolling wiped the old secret
// and recovery codes with nothing but a session cookie + CSRF token (fixed on the backend
// in 13-F1; this block is the frontend half). Now the page asks `/auth/me` for its OWN
// `mfa_enabled` flag (never a role) and shows ONLY the step-up form for a returning,
// already-enabled account; (re-)enrolling sits behind a separate, explicitly warned
// action whose form sends the current TOTP/recovery code as the backend's new proof.
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import QrCode from "@/components/QrCode";
import { ApiError, adminPing, me, mfaConfirm, mfaEnroll, mfaStepUp } from "@/lib/api";

type Stage = "loading" | "firstEnroll" | "returning" | "enrolling" | "confirmed";

export default function AccountSecurityPage() {
  const router = useRouter();
  const [stage, setStage] = useState<Stage>("loading");
  const [secret, setSecret] = useState<string | null>(null);
  const [provisioningUri, setProvisioningUri] = useState<string | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [stepUpCode, setStepUpCode] = useState("");
  const [stepUpMessage, setStepUpMessage] = useState<string | null>(null);
  const [reenrollOpen, setReenrollOpen] = useState(false);
  const [reenrollCode, setReenrollCode] = useState("");
  // T13.7.4 (user request 2026-11-17): shown only once this session has actually proven
  // itself against an admin-permissioned route (reuses the existing `/admin/ping`
  // step-up+role gate's pass/fail -- never a new "what is my role" read anywhere).
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function handleError(err: unknown) {
    if (err instanceof ApiError && err.status === 401) {
      router.replace("/login");
      return;
    }
    setError(err instanceof Error ? err.message : "Something went wrong");
  }

  useEffect(() => {
    // Same "CSR re-check" pattern as /account (T12.1.9): the middleware already redirected
    // a visitor with no session cookie at all before this ever renders.
    me()
      .then((info) => setStage(info.mfa_enabled ? "returning" : "firstEnroll"))
      .catch(() => {
        router.replace("/login");
      });
  }, [router]);

  async function probeAdmin() {
    try {
      await adminPing();
      setIsAdmin(true);
    } catch {
      setIsAdmin(false);
    }
  }

  async function onStartFirstEnroll() {
    setError(null);
    try {
      const res = await mfaEnroll();
      setSecret(res.secret);
      setProvisioningUri(res.provisioning_uri);
      setStage("enrolling");
    } catch (err) {
      handleError(err);
    }
  }

  async function onReenroll(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    try {
      const res = await mfaEnroll(reenrollCode.trim() || undefined);
      setSecret(res.secret);
      setProvisioningUri(res.provisioning_uri);
      setReenrollOpen(false);
      setReenrollCode("");
      setStage("enrolling");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        // The backend's own message ("invalid code") is already generic/safe to show --
        // nothing here distinguishes "wrong TOTP" from "reused recovery code" either.
        setError("That code didn't match. Nothing was changed.");
      } else {
        handleError(err);
      }
    }
  }

  async function onConfirm(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    try {
      const res = await mfaConfirm(confirmCode);
      setRecoveryCodes(res.recovery_codes);
      setStage("confirmed");
    } catch (err) {
      handleError(err);
    }
  }

  async function onStepUp(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setStepUpMessage(null);
    try {
      const res = await mfaStepUp(stepUpCode);
      setStepUpMessage(res.detail);
      await probeAdmin();
    } catch (err) {
      handleError(err);
    }
  }

  const stepUpForm = (
    <form onSubmit={onStepUp} className="flex flex-col gap-3 border border-vault-steel-dark p-4">
      <label className="text-sm text-vault-steel-light">
        Step up this session now (code or a recovery code):
        <input
          value={stepUpCode}
          onChange={(e) => setStepUpCode(e.target.value)}
          className="mt-1 w-full border border-vault-steel-dark bg-transparent px-3 py-2 text-vault-steel-light"
        />
      </label>
      <button
        type="submit"
        className="w-fit cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm tracking-[0.1em] text-vault-steel transition-colors hover:border-vault-steel hover:text-vault-steel-light"
      >
        Step up
      </button>
      {stepUpMessage && <p className="text-sm text-vault-steel">{stepUpMessage}</p>}
    </form>
  );

  const adminPanelLink = isAdmin && (
    <a
      href="/admin"
      className="w-fit cursor-pointer border border-vault-amber px-4 py-2 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright"
    >
      Admin panel
    </a>
  );

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">
        Two-factor authentication
      </h1>
      <p className="text-sm text-vault-steel">
        Required for admin routes. Set up a TOTP authenticator app (e.g. a password
        manager&apos;s built-in authenticator) below.
      </p>

      {error && <p className="text-sm text-vault-danger">{error}</p>}

      {stage === "loading" && <p className="text-sm text-vault-steel">Loading…</p>}

      {stage === "firstEnroll" && (
        <button
          type="button"
          onClick={onStartFirstEnroll}
          className="w-fit cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm tracking-[0.1em] text-vault-steel transition-colors hover:border-vault-steel hover:text-vault-steel-light"
        >
          Start enrolment
        </button>
      )}

      {stage === "returning" && (
        <>
          {stepUpForm}
          {adminPanelLink}

          <div className="border border-vault-danger p-4">
            <p className="text-sm text-vault-danger">
              Only replace two-factor authentication if you have lost access to your
              authenticator app or recovery codes. This invalidates your current secret and
              every existing recovery code.
            </p>
            {!reenrollOpen ? (
              <button
                type="button"
                onClick={() => setReenrollOpen(true)}
                className="mt-3 w-fit cursor-pointer border border-vault-danger bg-transparent px-4 py-2 text-sm tracking-[0.1em] text-vault-danger transition-colors hover:bg-vault-danger hover:text-vault-bg"
              >
                Replace my two-factor authentication
              </button>
            ) : (
              <form onSubmit={onReenroll} className="mt-3 flex flex-col gap-3">
                <label className="text-sm text-vault-steel-light">
                  Current 6-digit code or a recovery code:
                  <input
                    value={reenrollCode}
                    onChange={(e) => setReenrollCode(e.target.value)}
                    className="mt-1 w-full border border-vault-steel-dark bg-transparent px-3 py-2 text-vault-steel-light"
                  />
                </label>
                <div className="flex gap-2">
                  <button
                    type="submit"
                    className="cursor-pointer border border-vault-danger bg-transparent px-4 py-2 text-sm tracking-[0.1em] text-vault-danger transition-colors hover:bg-vault-danger hover:text-vault-bg"
                  >
                    Confirm and replace
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setReenrollOpen(false);
                      setReenrollCode("");
                    }}
                    className="cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm text-vault-steel transition-colors hover:text-vault-steel-light"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
          </div>
        </>
      )}

      {stage === "enrolling" && secret && provisioningUri && (
        <form
          onSubmit={onConfirm}
          className="flex flex-col gap-3 border border-vault-steel-dark p-4"
        >
          <p className="text-sm text-vault-steel-light">
            Scan this QR code with your authenticator app, or enter the secret manually below
            if your app cannot scan.
          </p>
          <div className="flex justify-center bg-white p-2" data-testid="mfa-qr-code">
            <QrCode data={provisioningUri} />
          </div>
          <p className="break-all text-xs text-vault-steel">Secret: {secret}</p>
          <p className="break-all text-xs text-vault-steel">URI: {provisioningUri}</p>
          <label className="text-sm text-vault-steel-light">
            Enter the 6-digit code your app shows:
            <input
              value={confirmCode}
              onChange={(e) => setConfirmCode(e.target.value)}
              maxLength={6}
              className="mt-1 w-full border border-vault-steel-dark bg-transparent px-3 py-2 text-vault-steel-light"
            />
          </label>
          <button
            type="submit"
            className="w-fit cursor-pointer border border-vault-amber px-4 py-2 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright"
          >
            CONFIRM
          </button>
        </form>
      )}

      {stage === "confirmed" && recoveryCodes && (
        <div className="flex flex-col gap-3 border border-vault-steel-dark p-4">
          <p className="text-sm text-vault-steel-light">
            Two-factor authentication is enabled. Save these single-use recovery codes
            somewhere safe -- they are shown only once:
          </p>
          <ul className="grid grid-cols-2 gap-1 text-xs text-vault-steel">
            {recoveryCodes.map((code) => (
              <li key={code} className="font-mono">
                {code}
              </li>
            ))}
          </ul>

          <form onSubmit={onStepUp} className="mt-2 flex flex-col gap-3">
            <label className="text-sm text-vault-steel-light">
              Step up this session now (code or a recovery code):
              <input
                value={stepUpCode}
                onChange={(e) => setStepUpCode(e.target.value)}
                className="mt-1 w-full border border-vault-steel-dark bg-transparent px-3 py-2 text-vault-steel-light"
              />
            </label>
            <button
              type="submit"
              className="w-fit cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm tracking-[0.1em] text-vault-steel transition-colors hover:border-vault-steel hover:text-vault-steel-light"
            >
              Step up
            </button>
            {stepUpMessage && <p className="text-sm text-vault-steel">{stepUpMessage}</p>}
          </form>
          {adminPanelLink}
        </div>
      )}
    </main>
  );
}
