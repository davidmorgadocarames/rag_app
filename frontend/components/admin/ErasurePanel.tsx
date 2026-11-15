"use client";

// T13.2.8: the erasure panel (opaque request ids, countdown to the 24h deadline, "Purge
// now"/"Retry" with an explicit two-click confirmation, the TF16 "continuing in the
// scheduled job" text straight from the backend's own action response) plus the
// "Erase an account" form (T13.1.8, DEBT-002) with its typed "ERASE" confirmation and the
// 409 (last remaining admin)/404 (no such account)/503 (busy, retry) responses each shown
// with their own distinct message -- never a submitted email echoed back anywhere.

import { useState } from "react";

import {
  ApiError,
  adminEraseUser,
  adminErasurePurgeNow,
  adminErasureRetry,
  type AdminErasureResponse,
  type ErasureRequestOut,
} from "@/lib/api";

const AMBER_THRESHOLD_SECONDS = 4 * 3600; // age > 20h of a 24h deadline

interface ErasurePanelProps {
  erasure: AdminErasureResponse | null;
  onChanged: () => void; // re-fetch the erasure list after an action completes
}

function countdownClass(row: ErasureRequestOut): string {
  if (row.status === "done") return "text-vault-steel";
  if (row.overdue) return "text-vault-danger";
  if (row.seconds_left < AMBER_THRESHOLD_SECONDS) return "text-vault-amber";
  return "text-vault-steel-light";
}

function formatHours(seconds: number): string {
  const hours = seconds / 3600;
  return `${hours >= 0 ? "" : "-"}${Math.abs(hours).toFixed(1)}h`;
}

type Action = "purge-now" | "retry";

export default function ErasurePanel({ erasure, onChanged }: ErasurePanelProps) {
  const [confirming, setConfirming] = useState<{ id: string; action: Action } | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function runAction(id: string, action: Action) {
    setConfirming(null);
    setActionError(null);
    try {
      const response =
        action === "purge-now" ? await adminErasurePurgeNow(id) : await adminErasureRetry(id);
      setActionMessage(response.detail);
      onChanged();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Something went wrong.");
    }
  }

  return (
    <section
      data-testid="admin-erasure-panel"
      className="flex flex-col gap-4 border border-vault-steel-dark p-4"
    >
      <h2 className="text-sm tracking-[0.18em] text-vault-steel-light">ERASURE</h2>

      {actionMessage && <p className="text-xs text-vault-amber">{actionMessage}</p>}
      {actionError && <p className="text-xs text-vault-danger">{actionError}</p>}

      <table className="w-full text-left text-xs text-vault-steel">
        <thead>
          <tr className="text-vault-steel-dark">
            <th className="py-1">Request</th>
            <th>Status</th>
            <th>Time left</th>
            <th>Attempts</th>
            <th>Last error</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {(erasure?.requests ?? []).map((row) => (
            <tr
              key={row.id}
              data-testid={`erasure-row-${row.id}`}
              className="border-t border-vault-steel-dark/40"
            >
              <td className="py-1 font-mono">{row.id.slice(0, 8)}</td>
              <td>{row.status}</td>
              <td className={countdownClass(row)}>{formatHours(row.seconds_left)}</td>
              <td>{row.attempts}</td>
              <td>{row.last_error_class ?? "-"}</td>
              <td>
                {row.status !== "done" && (
                  <ErasureActionButton
                    row={row}
                    confirming={confirming}
                    setConfirming={setConfirming}
                    runAction={runAction}
                  />
                )}
              </td>
            </tr>
          ))}
          {(erasure?.requests ?? []).length === 0 && (
            <tr>
              <td colSpan={6} className="py-2 text-vault-steel-dark">
                No erasure requests.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {erasure?.last_purger_run && (
        <p className="text-xs text-vault-steel-dark">
          Last purge job run: {erasure.last_purger_run.started_at} --{" "}
          {erasure.last_purger_run.requests_processed} processed,{" "}
          {erasure.last_purger_run.errors} error(s).
        </p>
      )}

      <EraseAccountForm onDone={onChanged} />
    </section>
  );
}

function ErasureActionButton({
  row,
  confirming,
  setConfirming,
  runAction,
}: {
  row: ErasureRequestOut;
  confirming: { id: string; action: Action } | null;
  setConfirming: (value: { id: string; action: Action } | null) => void;
  runAction: (id: string, action: Action) => void;
}) {
  const action: Action = row.status === "failed" ? "retry" : "purge-now";
  const label = action === "retry" ? "Retry" : "Purge now";
  const isConfirming = confirming?.id === row.id && confirming.action === action;

  if (isConfirming) {
    return (
      <span className="flex gap-2">
        <button
          type="button"
          onClick={() => runAction(row.id, action)}
          className="cursor-pointer border border-vault-danger px-2 py-1 text-vault-danger"
        >
          Confirm {label.toLowerCase()}?
        </button>
        <button
          type="button"
          onClick={() => setConfirming(null)}
          className="cursor-pointer border border-vault-steel-dark px-2 py-1"
        >
          Cancel
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setConfirming({ id: row.id, action })}
      className="cursor-pointer border border-vault-steel-dark px-2 py-1 hover:border-vault-steel"
    >
      {label}
    </button>
  );
}

function EraseAccountForm({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setResult(null);
    setError(null);
    setSubmitting(true);
    try {
      const response = await adminEraseUser(email, confirmation);
      setResult(`${response.detail} (request ${response.request_id})`);
      setEmail("");
      setConfirmation("");
      onDone();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setError("No account matches that email.");
      } else if (err instanceof ApiError && err.status === 409) {
        setError("That account cannot be erased: it is the only remaining admin account.");
      } else if (err instanceof ApiError && err.status === 503) {
        setError("The account is busy right now; nothing was deleted. Please try again.");
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      data-testid="erase-account-form"
      onSubmit={onSubmit}
      className="flex flex-col gap-2 border-t border-vault-steel-dark pt-4"
    >
      <h3 className="text-xs tracking-[0.14em] text-vault-steel-light">ERASE AN ACCOUNT</h3>
      <label className="text-xs text-vault-steel">
        Account email
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mt-1 w-full border border-vault-steel-dark bg-transparent px-2 py-1 text-vault-steel-light"
        />
      </label>
      <label className="text-xs text-vault-steel">
        Type ERASE to confirm
        <input
          required
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          className="mt-1 w-full border border-vault-steel-dark bg-transparent px-2 py-1 text-vault-steel-light"
        />
      </label>
      <button
        type="submit"
        disabled={submitting}
        className="w-fit cursor-pointer border border-vault-danger px-3 py-1 text-xs tracking-[0.1em] text-vault-danger disabled:opacity-50"
      >
        Erase account
      </button>
      {result && <p className="text-xs text-vault-amber">{result}</p>}
      {error && <p className="text-xs text-vault-danger">{error}</p>}
    </form>
  );
}
