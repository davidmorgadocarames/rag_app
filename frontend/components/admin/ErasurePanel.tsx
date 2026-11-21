"use client";

// T13.2.8/T13.8.8 (13-G3): the erasure panel (opaque request ids, countdown to the 24h
// deadline, "Purge now"/"Retry" with an explicit two-click confirmation, the TF16 "continuing
// in the scheduled job" text straight from the backend's own action response) plus the
// "Erase an account" form (T13.1.8, DEBT-002) with its typed "ERASE" confirmation and the
// 409 (last remaining admin)/404 (no such account)/503 (busy, retry) responses each shown
// with their own distinct message -- never a submitted email echoed back anywhere.
//
// T13.8.8: a "done" row now shows "Completed <completed_at>" -- a fact about the past -- never
// a countdown (which only ever makes sense for an OPEN request still racing the deadline).

import { useState } from "react";

import {
  ApiError,
  adminEraseUser,
  adminErasurePurgeNow,
  adminErasureRetry,
  type AdminErasureResponse,
  type ErasureRequestOut,
} from "@/lib/api";
// T13.12.2 (13-J3): shared with the "Erasure queue" KPI tile's own near-deadline count
// (AdminDashboard.tsx/adminKpi.ts) -- a single threshold, never two copies that could drift.
import { AMBER_THRESHOLD_SECONDS } from "@/lib/adminKpi";

import StatusBadge from "./StatusBadge";

interface ErasurePanelProps {
  erasure: AdminErasureResponse | null;
  onChanged: () => void; // re-fetch the erasure list after an action completes
}

// T13.J.1: countdown colours now mirror the mockup's own `e.timeColor` values exactly
// (done -> muted, retrying/near-deadline -> warn, overdue -> crit, otherwise -> strong text)
// instead of the generic danger/steel tokens 13-G shipped with.
function countdownClass(row: ErasureRequestOut): string {
  if (row.status === "done") return "text-vault-text-muted";
  if (row.overdue) return "text-vault-crit";
  if (row.seconds_left < AMBER_THRESHOLD_SECONDS) return "text-vault-warn";
  return "text-vault-text-strong";
}

function formatHours(seconds: number): string {
  const hours = seconds / 3600;
  return `${hours >= 0 ? "" : "-"}${Math.abs(hours).toFixed(1)}h`;
}

// T13.12.2 (13-J3): the mockup's "Rows left" column (Main.dc.html line 272/283) -- the total
// rows still to purge across every `PURGE_STEPS` table, summed from the real
// `rows_remaining` map the backend already returns (replaces the old "Last error" column,
// which the mockup does not show at all; `last_error_class` stays a real field on the wire,
// just not its own column here any more).
function rowsLeft(row: ErasureRequestOut): number {
  return Object.values(row.rows_remaining).reduce((total, count) => total + count, 0);
}

/** T13.8.8: a done row's own time column -- its completion fact, never a countdown (which
 * stops meaning anything once the request is no longer open). `completed_at` is optional on
 * the wire (older backends before T13.8.3(b)) -- falls back to a plain "Completed" rather than
 * fabricating a timestamp. */
function timeCellText(row: ErasureRequestOut): string {
  if (row.status === "done") {
    return row.completed_at ? `Completed ${row.completed_at}` : "Completed";
  }
  return formatHours(row.seconds_left);
}

function statusTone(status: string): "ok" | "warn" | "crit" | "off" {
  if (status === "done") return "off";
  if (status === "failed") return "crit";
  if (status === "running") return "warn";
  return "ok"; // "pending"
}

function statusLabel(status: string): string {
  return status.length === 0 ? status : status[0].toUpperCase() + status.slice(1);
}

// T13.12.2 (13-J3): the mockup's own `<th>` style (Main.dc.html line 269-274) -- 12px
// uppercase, 0.06em tracking, muted, 500 weight, 8px/6px padding, a `--line` bottom border.
function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th
      scope="col"
      className={`border-b border-vault-line px-1.5 py-2 text-xs font-medium uppercase tracking-[0.06em] text-vault-text-muted ${
        right ? "text-right" : "text-left"
      }`}
    >
      {children}
    </th>
  );
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
      className="flex flex-col gap-3.5 rounded-admin-card border border-vault-line bg-vault-surface p-[18px]"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm tracking-[0.18em] text-vault-text-strong">
          ERASURE REQUESTS &middot; 24 H SLA
        </h2>
        <span className="text-xs text-vault-text-muted">
          Request IDs only &mdash; no e-mail addresses shown
        </span>
      </div>

      {actionMessage && <p className="text-xs text-vault-amber-bright">{actionMessage}</p>}
      {actionError && <p className="text-xs text-vault-danger-text">{actionError}</p>}

      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] border-collapse text-left text-sm">
          <thead>
            <tr>
              <Th>Request</Th>
              <Th>Status</Th>
              <Th>Deadline</Th>
              <Th right>Rows left</Th>
              <Th right>Attempts</Th>
              <th scope="col" className="border-b border-vault-line px-1.5 py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(erasure?.requests ?? []).map((row) => (
              <tr key={row.id} data-testid={`erasure-row-${row.id}`}>
                <td className="border-b border-vault-row-divider px-1.5 py-2.5 font-mono text-vault-text-strong">
                  {row.id.slice(0, 8)}
                </td>
                <td className="border-b border-vault-row-divider px-1.5 py-2.5">
                  <StatusBadge tone={statusTone(row.status)} label={statusLabel(row.status)} />
                </td>
                <td
                  className={`border-b border-vault-row-divider px-1.5 py-2.5 font-mono ${countdownClass(row)}`}
                >
                  {timeCellText(row)}
                </td>
                <td className="border-b border-vault-row-divider px-1.5 py-2.5 text-right font-mono text-vault-text-strong">
                  {rowsLeft(row)}
                </td>
                <td className="border-b border-vault-row-divider px-1.5 py-2.5 text-right font-mono text-vault-text-strong">
                  {row.attempts}
                </td>
                <td className="border-b border-vault-row-divider px-1.5 py-2.5 text-right">
                  {row.status !== "done" && (
                    <ErasureActionButton
                      row={row}
                      confirming={confirming}
                      setConfirming={setConfirming}
                      runAction={runAction}
                    />
                  )}
                  {row.status === "done" && (
                    <span className="text-xs text-vault-text-muted">nothing to do</span>
                  )}
                </td>
              </tr>
            ))}
            {(erasure?.requests ?? []).length === 0 && (
              <tr>
                <td colSpan={6} className="px-1.5 py-2 text-vault-text-muted">
                  No erasure requests.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {erasure?.last_purger_run && (
        <p className="text-xs text-vault-text-muted">
          Last purge job run: {erasure.last_purger_run.started_at} --{" "}
          {erasure.last_purger_run.requests_processed} processed,{" "}
          {erasure.last_purger_run.errors} error(s).
        </p>
      )}

      <p className="text-xs text-vault-text-muted">
        Done rows show when they finished, never a countdown. &quot;Purge now&quot; only runs
        the batch delete; requests are created with &quot;Erase account&quot;.
      </p>

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
          className="cursor-pointer border border-vault-danger px-2 py-1 text-vault-danger-text"
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

  // T13.12.2: matches the mockup's own two button kinds exactly (Main.dc.html's erasure
  // table) -- "Purge now" is the one FILLED amber primary action on this whole dashboard,
  // "Retry" is the same outline secondary style every other admin button uses.
  const isPrimary = action === "purge-now";
  return (
    <button
      type="button"
      onClick={() => setConfirming({ id: row.id, action })}
      className={
        isPrimary
          ? "min-h-9 cursor-pointer rounded-admin-btn bg-vault-amber px-3 py-1.5 font-semibold text-vault-bg"
          : "min-h-9 cursor-pointer rounded-admin-btn border border-vault-steel-dark px-3 py-1.5 text-vault-amber-bright hover:border-vault-steel"
      }
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
      className="flex flex-col gap-2 border-t border-vault-line pt-4"
    >
      <h3 className="text-xs tracking-[0.14em] text-vault-text-strong">ERASE AN ACCOUNT</h3>
      <label className="text-xs text-vault-text-muted">
        Account email
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mt-1 w-full border border-vault-steel-dark bg-transparent px-2 py-1 text-vault-steel-light"
        />
      </label>
      <label className="text-xs text-vault-text-muted">
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
        className="w-fit cursor-pointer border border-vault-danger px-3 py-1 text-xs tracking-[0.1em] text-vault-danger-text disabled:opacity-50"
      >
        Erase account
      </button>
      {result && <p className="text-xs text-vault-amber-bright">{result}</p>}
      {error && <p className="text-xs text-vault-danger-text">{error}</p>}
    </form>
  );
}
