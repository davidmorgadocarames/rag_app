"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { deleteAccount, me, resendVerification, type UserInfo } from "@/lib/api";
import { clearToken, getToken } from "@/lib/session";

export default function AccountPage() {
  const router = useRouter();
  const [user, setUser] = useState<UserInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [verifyMsg, setVerifyMsg] = useState<string | null>(null);
  const [verifyLink, setVerifyLink] = useState<string | null>(null);

  async function onResend() {
    const token = getToken();
    if (!token) return;
    setVerifyMsg(null);
    setVerifyLink(null);
    try {
      const res = await resendVerification(token);
      setVerifyMsg(res.detail);
      setVerifyLink(res.verification_link);
    } catch (err) {
      setVerifyMsg(err instanceof Error ? err.message : "Something went wrong");
    }
  }

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    me(token)
      .then(setUser)
      .catch(() => {
        clearToken();
        router.replace("/login");
      });
  }, [router]);

  function signOut() {
    clearToken();
    router.push("/login");
  }

  async function onDelete() {
    const token = getToken();
    if (!token) return;
    try {
      await deleteAccount(token);
      clearToken();
      router.push("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">Account</h1>
      {user ? (
        <div className="flex flex-col gap-1">
          <p className="text-vault-steel-light">{user.email}</p>
          <p className="text-sm text-vault-steel">
            Email verified: {user.email_verified ? "yes" : "no"}
          </p>
        </div>
      ) : (
        <p className="text-sm text-vault-steel">Loading…</p>
      )}

      {user && !user.email_verified && (
        <div className="border border-vault-steel-dark p-4">
          <p className="text-sm text-vault-steel-light">Your email isn&apos;t verified yet.</p>
          <button
            type="button"
            onClick={onResend}
            className="mt-3 cursor-pointer border border-vault-amber px-4 py-2 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright"
          >
            RESEND VERIFICATION
          </button>
          {verifyMsg && <p className="mt-3 text-sm text-vault-steel">{verifyMsg}</p>}
          {verifyLink && (
            <a
              href={verifyLink}
              target="_blank"
              rel="noreferrer"
              className="mt-1 block break-all text-sm text-vault-amber underline hover:text-vault-amber-bright"
            >
              Verify now (dev link)
            </a>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={signOut}
        className="w-fit cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm tracking-[0.1em] text-vault-steel transition-colors hover:border-vault-steel hover:text-vault-steel-light"
      >
        Sign out
      </button>

      <div className="mt-4 border border-vault-danger p-4">
        <p className="font-medium tracking-[0.06em] text-vault-danger">Delete my data</p>
        <p className="mt-1 text-sm text-vault-steel">
          Erases your account and all associated data. This is irreversible.
        </p>
        {error && <p className="mt-2 text-sm text-vault-danger">{error}</p>}
        {confirming ? (
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={onDelete}
              className="cursor-pointer border border-vault-danger bg-transparent px-4 py-2 text-sm tracking-[0.1em] text-vault-danger transition-colors hover:bg-vault-danger hover:text-vault-bg"
            >
              Confirm deletion
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="cursor-pointer border border-vault-steel-dark px-4 py-2 text-sm text-vault-steel transition-colors hover:text-vault-steel-light"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="mt-3 cursor-pointer border border-vault-danger bg-transparent px-4 py-2 text-sm tracking-[0.1em] text-vault-danger transition-colors hover:bg-vault-danger hover:text-vault-bg"
          >
            Delete my data
          </button>
        )}
      </div>
    </main>
  );
}
