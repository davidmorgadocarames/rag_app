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

import { useRouter } from "next/navigation";
import { useState } from "react";

import QrCode from "@/components/QrCode";
import { ApiError, mfaConfirm, mfaEnroll, mfaStepUp } from "@/lib/api";

type Stage = "idle" | "enrolled" | "confirmed";

export default function AccountSecurityPage() {
  const router = useRouter();
  const [stage, setStage] = useState<Stage>("idle");
  const [secret, setSecret] = useState<string | null>(null);
  const [provisioningUri, setProvisioningUri] = useState<string | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [stepUpCode, setStepUpCode] = useState("");
  const [stepUpMessage, setStepUpMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function handleError(err: unknown) {
    if (err instanceof ApiError && err.status === 401) {
      router.replace("/login");
      return;
    }
    setError(err instanceof Error ? err.message : "Something went wrong");
  }

  async function onEnroll() {
    setError(null);
    try {
      const res = await mfaEnroll();
      setSecret(res.secret);
      setProvisioningUri(res.provisioning_uri);
      setStage("enrolled");
    } catch (err) {
      handleError(err);
    }
  }

  async function onConfirm() {
    setError(null);
    try {
      const res = await mfaConfirm(confirmCode);
      setRecoveryCodes(res.recovery_codes);
      setStage("confirmed");
    } catch (err) {
      handleError(err);
    }
  }

  async function onStepUp() {
    setError(null);
    setStepUpMessage(null);
    try {
      const res = await mfaStepUp(stepUpCode);
      setStepUpMessage(res.detail);
    } catch (err) {
      handleError(err);
    }
  }

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

      {stage === "idle" && (
        <button
          type="button"
          onClick={onEnroll}
          className="w-fit cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm tracking-[0.1em] text-vault-steel transition-colors hover:border-vault-steel hover:text-vault-steel-light"
        >
          Start enrolment
        </button>
      )}

      {stage === "enrolled" && secret && provisioningUri && (
        <div className="flex flex-col gap-3 border border-vault-steel-dark p-4">
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
            type="button"
            onClick={onConfirm}
            className="w-fit cursor-pointer border border-vault-amber px-4 py-2 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright"
          >
            CONFIRM
          </button>
        </div>
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

          <label className="mt-2 text-sm text-vault-steel-light">
            Step up this session now (code or a recovery code):
            <input
              value={stepUpCode}
              onChange={(e) => setStepUpCode(e.target.value)}
              className="mt-1 w-full border border-vault-steel-dark bg-transparent px-3 py-2 text-vault-steel-light"
            />
          </label>
          <button
            type="button"
            onClick={onStepUp}
            className="w-fit cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm tracking-[0.1em] text-vault-steel transition-colors hover:border-vault-steel hover:text-vault-steel-light"
          >
            Step up
          </button>
          {stepUpMessage && <p className="text-sm text-vault-steel">{stepUpMessage}</p>}
        </div>
      )}
    </main>
  );
}
