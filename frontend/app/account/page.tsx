"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { deleteAccount, me, type UserInfo } from "@/lib/api";
import { clearToken, getToken } from "@/lib/session";

export default function AccountPage() {
  const router = useRouter();
  const [user, setUser] = useState<UserInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

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
      <h1 className="text-2xl font-bold tracking-tight">Account</h1>
      {user ? (
        <div className="flex flex-col gap-1">
          <p className="text-slate-600 dark:text-slate-300">{user.email}</p>
          <p className="text-sm text-slate-500">
            Email verified: {user.email_verified ? "yes" : "no"}
          </p>
        </div>
      ) : (
        <p className="text-sm text-slate-500">Loading…</p>
      )}

      <button
        type="button"
        onClick={signOut}
        className="w-fit rounded-lg border border-slate-300 px-4 py-2 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
      >
        Sign out
      </button>

      <div className="mt-4 rounded-lg border border-red-300 p-4 dark:border-red-800">
        <p className="font-medium text-red-700 dark:text-red-400">Delete my data</p>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Erases your account and all associated data. This is irreversible.
        </p>
        {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
        {confirming ? (
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={onDelete}
              className="rounded-lg bg-red-600 px-4 py-2 font-medium text-white hover:bg-red-500"
            >
              Confirm deletion
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-lg border border-slate-300 px-4 py-2 dark:border-slate-700"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="mt-3 rounded-lg bg-red-600 px-4 py-2 font-medium text-white hover:bg-red-500"
          >
            Delete my data
          </button>
        )}
      </div>
    </main>
  );
}
