// Typed client for the SecRAG backend API, called ONLY through the BFF (T12.1.7, P12-D1):
// relative /api/... paths (no build-time backend URL, no NEXT_PUBLIC_API_URL), same-origin
// cookies for auth (no token argument anywhere -- the browser attaches the HttpOnly session
// cookie automatically) and a CSRF header read from the readable, non-HttpOnly
// `__Host-secrag_csrf` cookie on every state-changing request.

import { consumeChatStream, refusedStream } from "./chatStream.ts";
import { classifyWakeSignal, WakeUpError, withWakeRetry } from "./wakeUp.ts";

// --- CSRF (P12-D2): pure, DOM-light helpers so they stay directly testable -----------------

export const CSRF_COOKIE_NAME = "__Host-secrag_csrf";

/** The raw value of cookie `name` out of a `document.cookie`-shaped header string. */
export function readCookie(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const i = part.indexOf("=");
    if (i < 0) continue;
    if (part.slice(0, i).trim() === name) return part.slice(i + 1).trim() || null;
  }
  return null;
}

const CSRF_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

function currentCsrfToken(): string | null {
  if (typeof document === "undefined") return null; // SSR / no browser context
  return readCookie(document.cookie, CSRF_COOKIE_NAME);
}

export interface Citation {
  marker: number;
  chunk_uid: string;
  heading: string;
  version: string;
  effective_date: string | null;
}

export interface ChatResponse {
  answer: string;
  abstained: boolean;
  grounded: boolean;
  citations: Citation[];
}

export interface UserInfo {
  id: string;
  email: string;
  email_verified: boolean;
  // T13.7.4: the account's OWN MFA status (never a role -- see mfaEnroll's own comment on
  // why "is this account an admin" is deliberately never read from here).
  mfa_enabled: boolean;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatDone {
  conversation_id: string;
  answer: string;
  abstained: boolean;
  grounded: boolean;
  citations: Citation[];
  usage: Usage;
  conversation_total_tokens: number;
  // T14.5.4: estimated cost of this turn's tracked usage, for the active provider -- always
  // 0 for local Ollama.
  estimated_cost: number;
  cost_currency: string;
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  total_tokens: number;
  // true when the conversation cannot be decrypted (listed with a placeholder title)
  unreadable: boolean;
}

export interface StoredMessage {
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  abstained: boolean;
  grounded: boolean;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string;
  // an assistant error marker: that turn failed; content is the message shown
  error: boolean;
}

export interface ConversationDetail {
  id: string;
  title: string;
  messages: StoredMessage[];
}

export class ApiError extends Error {
  readonly status?: number;
  // A plain assignment, not a TS "parameter property" (`readonly status?: number` straight
  // in the constructor signature): Node's --test type-stripping (no full tsc) rejects that
  // syntax outright (ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX) the moment anything imports this
  // module directly, which api.test.ts now does.
  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

// T13.9.1 (DEBT-056, DEC-048): hooks a caller passes through to show (and clear) a shared
// "Waking the server up…" state around this one call -- see lib/wakeUp.ts for the retry
// policy itself. `idempotent` overrides the method-based default (every GET is idempotent
// by default; everything else is not, UNLESS the caller knows better -- see `login` below).
export interface WakeHooks {
  idempotent?: boolean;
  onWaking?: () => void;
  onRecovered?: () => void;
  // Test-only override of the retry budget/clock, threaded straight through to
  // withWakeRetry -- never set in product code.
  budgetMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

async function doRequest<T>(path: string, options: RequestInit, method: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (CSRF_METHODS.has(method)) {
    const csrf = currentCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      method,
      headers,
      credentials: "same-origin", // the BFF is always same-origin: send the session cookie
    });
  } catch {
    // The browser's own fetch to our same-origin BFF route never even got a response --
    // nothing could have reached the backend either. ApiError with status === undefined is
    // classified "safe" below (classifyWakeSignal(null, ...)).
    throw new ApiError("The server cannot be reached. Please try again.", undefined);
  }
  if (!response.ok) {
    let detail = `request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // keep the default message
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  wake: WakeHooks = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  return withWakeRetry({
    attempt: () => doRequest<T>(path, options, method),
    classifyError: (err) =>
      err instanceof ApiError ? classifyWakeSignal(err.status ?? null, err.message) : "none",
    idempotent: wake.idempotent ?? method === "GET",
    onWaking: wake.onWaking,
    onRecovered: wake.onRecovered,
    budgetMs: wake.budgetMs,
    now: wake.now,
    sleep: wake.sleep,
  });
}

// T12.4.4: a generic 202, no session/token any more -- the caller never learns whether the
// address was new or already registered (only the real owner, by email, does).
export function register(email: string, password: string): Promise<{ detail: string }> {
  return request("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
}

// T13.9.1 (reworded, DA-13-H-2): `idempotent: true` -- unlike a chat question, repeating a
// login has no HARMFUL side effect, so a cold-start-shaped failure here is always retried
// automatically with backoff, never just a single soft fail. What actually happens on a
// retry is NOT "the same session cookie either way": backend/src/rag_app/api/auth.py's
// `login()` calls `session_service.create_session` on every successful call, so a retry
// after a lost response creates a second, equally valid session -- harmless, just one more
// row to expire later on its own idle/absolute timeout. It cannot double-count the lockout
// counter either: a real 401 is classified "none" by `classifyWakeSignal` and is never
// retried at all, and that check runs BEFORE `create_session` on the backend.
export function login(email: string, password: string, wake: WakeHooks = {}): Promise<void> {
  return request(
    "/auth/login",
    { method: "POST", body: JSON.stringify({ email, password }) },
    { ...wake, idempotent: true },
  );
}

export function logout(): Promise<{ detail: string }> {
  return request("/auth/logout", { method: "POST" });
}

export function me(wake: WakeHooks = {}): Promise<UserInfo> {
  return request("/auth/me", {}, wake);
}

// T12.4.4: reached by email now, not an authenticated session (a freshly registered,
// unverified account has none) -- same generic response whatever the outcome.
export function resendVerification(email: string): Promise<{ detail: string }> {
  return request("/auth/resend-verification", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

// T12.4.5: the /verify page's button POSTs the token from its URL -- a plain GET (the
// email link itself) only ever renders the page, so a mail scanner pre-fetching it can
// never consume the token.
export function verifyEmail(token: string): Promise<{ detail: string }> {
  return request("/auth/verify", { method: "POST", body: JSON.stringify({ token }) });
}

// T12.4.5 (frontend shell) / T12.4.6 (12b-C, the backend route + BFF allow-list entry):
// the /reset page's button POSTs the token from its URL plus the new password.
export function confirmPasswordReset(token: string, password: string): Promise<{ detail: string }> {
  return request("/auth/reset", { method: "POST", body: JSON.stringify({ token, password }) });
}

// T12.4.6: the forgot-password form's submit -- a generic response whatever the outcome
// (no account, unverified, or a real reset email sent); see GENERIC_FORGOT_PASSWORD_MESSAGE
// on the backend.
export function forgotPassword(email: string): Promise<{ detail: string }> {
  return request("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email }) });
}

export function deleteAccount(): Promise<{ detail: string }> {
  return request("/account", { method: "DELETE" });
}

// --- admin MFA (T12.3.4/T12.3.5; written in 12b-A, frontend verification is Phase 2) ------

export interface MfaEnrollResponse {
  secret: string;
  provisioning_uri: string;
}

export interface MfaConfirmResponse {
  recovery_codes: string[];
}

// T13.7.4/DEBT-051: `code` is required only to re-enrol an ALREADY-ENABLED record (the
// backend's `MfaEnrollRequest.code`, DEC-041) -- omitted entirely (no body at all, never an
// empty string: the backend's `code: str | None` would reject a blank string on length
// alone) for first-time enrolment, where there is nothing yet to prove.
export function mfaEnroll(code?: string): Promise<MfaEnrollResponse> {
  return request("/account/mfa/enroll", {
    method: "POST",
    body: code ? JSON.stringify({ code }) : undefined,
  });
}

export function mfaConfirm(code: string): Promise<MfaConfirmResponse> {
  return request("/account/mfa/confirm", { method: "POST", body: JSON.stringify({ code }) });
}

export function mfaStepUp(code: string): Promise<{ detail: string }> {
  return request("/account/mfa/step-up", { method: "POST", body: JSON.stringify({ code }) });
}

export function adminPing(): Promise<{ detail: string }> {
  return request("/admin/ping");
}

// --- admin dashboard (T13.1-T13.4): typed responses, same shapes as the backend's --------

export interface FeatureHealthOut {
  id: string;
  name: string;
  phase: string;
  status: string;
  probe_status: string | null;
  probe_detail: string | null;
  metrics: Record<string, number | null>;
  dev_only: boolean;
  azure_only: boolean;
  // T13.8.9 (DEBT-053): whether THIS stack is expected to be running this feature right now
  // (computed server-side from the live settings) -- see adminTriage.ts::featureStatus for
  // how this replaces the static dev_only/azure_only check for the Off-here/Critical split.
  expected_here: boolean;
  // T13.8.6 (13-G2): the registry's own docs/ADR reference, for the feature drawer's docs link.
  docs: string;
}

// TF14: the server-side decision the dashboard reads to pick SSE vs. polling -- never a
// frontend build-time variable.
export type AdminLiveMode = "sse" | "polling";

export interface AdminFeaturesResponse {
  features: FeatureHealthOut[];
  live_mode: AdminLiveMode;
  // DA-13-E-2: banner alerts from the same probe sweep (one sweep per polling tick).
  alerts: AdminAlertOut[];
}

export interface AdminMetricsResponse {
  values: Record<string, number | null>;
}

export interface StageLatencyOut {
  stage: string;
  count: number;
  avg_seconds: number | null;
  p50_seconds: number | null;
  p95_seconds: number | null;
}

// T13.8.2: one atomic step's latency, nested under its UI pipeline stage below -- a
// DIFFERENT, higher-level "stage" grouping than StageLatencyOut's own (e.g. "retrieving"
// covers both the old "embed" and "hybrid_search" stage names).
export interface StepLatencyOut {
  stage: string;
  step: string;
  count: number;
  avg_seconds: number | null;
  p50_seconds: number | null;
  p95_seconds: number | null;
}

export interface PipelineStageLatencyOut {
  stage: string;
  steps: StepLatencyOut[];
}

export interface AdminLatencyResponse {
  stages: StageLatencyOut[];
  // Additive (13-G1 backend): the atomic-step waterfall behind the Pipeline view's
  // expandable rows (13-G2/13-G3 frontend, this replica only). Optional so a response
  // observed before this field existed still type-checks.
  pipeline?: PipelineStageLatencyOut[];
  // T14.5.4: aggregate estimated token cost, this replica since it last started -- an
  // aggregate only, never per-user/per-conversation. Optional for the same reason as
  // `pipeline` above.
  estimated_cost_total?: number;
  cost_currency?: string;
}

export interface EvalMetricOut {
  key: string;
  value: number | null;
  floor: number | null;
  baseline: number | null;
  below_floor: boolean;
  regressed: boolean;
}

export interface AdminEvalResponse {
  last_run_at: string | null;
  metrics: EvalMetricOut[];
  passed: boolean;
}

export interface ErasureRequestOut {
  id: string;
  requested_at: string;
  age_seconds: number;
  seconds_left: number;
  status: string;
  rows_remaining: Record<string, number>;
  attempts: number;
  last_error_class: string | null;
  overdue: boolean;
  // T13.8.3(b): set once status is "done" (null while open).
  completed_at?: string | null;
}

export interface LastPurgerRunOut {
  started_at: string;
  finished_at: string | null;
  requests_processed: number;
  errors: number;
}

export interface AdminErasureResponse {
  requests: ErasureRequestOut[];
  last_purger_run: LastPurgerRunOut | null;
}

// T13.5.2 (R6-1, P13-D4): pre-computed server-side (never a client-side threshold) -- the
// same alerts shape whether read via /admin/alerts (polling, Azure T13.4.2) or the SSE
// snapshot/heartbeat (local).
export type AdminAlertSeverity = "amber" | "red";

export interface AdminAlertOut {
  code: string;
  severity: AdminAlertSeverity;
  message: string;
  // T13.8.3(a): registry feature id(s) this alert relates to -- lets the drawer for a
  // feature list its related alerts without a second lookup table.
  feature_ids?: string[];
}

export interface AdminAlertsResponse {
  alerts: AdminAlertOut[];
}

export interface ErasureActionResponse {
  detail: string;
  request_id: string;
}

export interface AdminEraseUserResponse {
  detail: string;
  request_id: string;
}

export function adminFeatures(wake: WakeHooks = {}): Promise<AdminFeaturesResponse> {
  return request("/admin/features", {}, wake);
}

export function adminMetrics(): Promise<AdminMetricsResponse> {
  return request("/admin/metrics");
}

export function adminLatency(wake: WakeHooks = {}): Promise<AdminLatencyResponse> {
  return request("/admin/latency", {}, wake);
}

export function adminEval(wake: WakeHooks = {}): Promise<AdminEvalResponse> {
  return request("/admin/eval", {}, wake);
}

export function adminErasure(wake: WakeHooks = {}): Promise<AdminErasureResponse> {
  return request("/admin/erasure", {}, wake);
}

export function adminAlerts(): Promise<AdminAlertsResponse> {
  return request("/admin/alerts");
}

export function adminErasurePurgeNow(requestId: string): Promise<ErasureActionResponse> {
  return request(`/admin/erasure/${requestId}/purge-now`, { method: "POST" });
}

export function adminErasureRetry(requestId: string): Promise<ErasureActionResponse> {
  return request(`/admin/erasure/${requestId}/retry`, { method: "POST" });
}

export function adminEraseUser(
  email: string,
  confirmation: string,
): Promise<AdminEraseUserResponse> {
  return request("/admin/users/erase", {
    method: "POST",
    body: JSON.stringify({ email, confirmation }),
  });
}

// --- defence lab (T13.3.5, local only -- DEC-028) -------------------------

export interface DefenceProbeResultOut {
  probe_id: string;
  expected_outcome: string;
  observed_outcome: string;
  passed: boolean;
  // T13.J4-fix (DA-13-J3J4-2): the mockup's own `p.name`/`p.what` -- optional on the wire
  // (an older backend, or a seeded e2e fixture, may omit them); the panel falls back to the
  // raw `probe_id` when absent.
  name?: string;
  description?: string;
}

export interface DefenceLabRunResponse {
  detail: string;
  git_sha: string;
  results: DefenceProbeResultOut[];
  all_matched: boolean;
}

export interface DefenceRunOut {
  probe_id: string;
  git_sha: string;
  expected_outcome: string;
  observed_outcome: string;
  passed: boolean;
  run_at: string;
  name?: string;
  description?: string;
}

export interface DefenceRunsResponse {
  runs: DefenceRunOut[];
}

export function adminLabRun(): Promise<DefenceLabRunResponse> {
  return request("/admin/lab/run", { method: "POST" });
}

// T13.J4-fix (DA-13-J3J4-1): runs exactly ONE catalogue probe -- the mockup's own per-card
// "Run probe" button. Same admin-only/step-up/CSRF gating as adminLabRun() above (the
// backend's POST /admin/lab/run/{probe_id}), just scoped to a single probe id.
export function adminLabRunProbe(probeId: string): Promise<DefenceLabRunResponse> {
  return request(`/admin/lab/run/${encodeURIComponent(probeId)}`, { method: "POST" });
}

export function adminLabRuns(): Promise<DefenceRunsResponse> {
  return request("/admin/lab/runs");
}

// --- conversations -------------------------------------------------------

export function listConversations(): Promise<ConversationSummary[]> {
  return request("/conversations");
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request(`/conversations/${id}`);
}

export function renameConversation(id: string, title: string): Promise<{ detail: string }> {
  return request(`/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
}

export function deleteConversation(id: string): Promise<{ detail: string }> {
  return request(`/conversations/${id}`, { method: "DELETE" });
}

// --- streaming chat (Server-Sent Events over fetch, through the BFF) ----------------------

export interface StreamCallbacks {
  // First event of every stream: the conversation the message belongs to (null only when
  // nothing could be stored, e.g. the data key is unavailable for a new conversation).
  onConversation?: (id: string) => void;
  onStage?: (stage: string) => void;
  // T13.12.5 (DEBT-062, widened T14.4.1): see lib/chatStream.ts's own StreamHandlers.onStep.
  onStep?: (stage: string, step: string, phase: "start" | "end", detail?: string) => void;
  onToken?: (text: string) => void;
  onDone?: (data: ChatDone) => void;
  // Always called when the turn fails, with the conversation id when known;
  // dropConversation: the conversation is gone (404) — the next message starts a new one.
  onError?: (detail: string, conversationId: string | null, dropConversation?: boolean) => void;
  // T13.9.1 (DEBT-056): the shared wake-up state (lib/wakeUp.ts). onWaking/onRecovered
  // bracket an automatic retry loop, which only ever happens BEFORE any token has streamed,
  // for a connect-level failure that provably never reached the backend (classic
  // scale-from-zero: no replica is listening yet). onWakeFailed fires INSTEAD OF onError
  // when the retry budget ran out, or a single ambiguous cold-start-shaped failure (a BFF
  // timeout, or a pass-through 502/503/504 -- the question may already be mid-flight on the
  // backend) hit: the caller must offer an explicit Retry of the SAME question rather than
  // show it as an ordinary answer-failed error, and must never resend it automatically.
  onWaking?: () => void;
  onRecovered?: () => void;
  onWakeFailed?: () => void;
}

/** Carries the BFF's response (or its absence) through withWakeRetry's classifier. */
class StreamRefusedError extends Error {
  readonly status: number | null;
  readonly detail: string | null;
  constructor(status: number | null, detail: string | null) {
    super(detail ?? `request failed (${status ?? "no response"})`);
    this.status = status;
    this.detail = detail;
  }
}

// Test-only tuning of the retry loop (budget/clock/sleep) -- see lib/wakeUp.ts's own
// WakeRetryOptions; never set outside lib/chatStream-adjacent tests.
export interface StreamWakeTuning {
  budgetMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

export async function streamChat(
  body: { question: string; conversation_id?: string; version?: string },
  cb: StreamCallbacks,
  wakeTuning: StreamWakeTuning = {},
): Promise<void> {
  const csrf = currentCsrfToken();

  async function attempt(): Promise<void> {
    let response: Response;
    try {
      response = await fetch("/api/chat/stream", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          ...(csrf ? { "X-CSRF-Token": csrf } : {}),
        },
        body: JSON.stringify(body),
      });
    } catch {
      throw new StreamRefusedError(null, null);
    }
    if (!response.ok || !response.body) {
      let detail = `request failed (${response.status})`;
      try {
        const err = await response.json();
        if (typeof err?.detail === "string") detail = err.detail;
      } catch {
        // keep default
      }
      throw new StreamRefusedError(response.status, detail);
    }
    await consumeChatStream(response.body, {
      onConversation: cb.onConversation,
      onStage: cb.onStage,
      onStep: cb.onStep,
      onToken: cb.onToken,
      onDone: (event) => cb.onDone?.(event as unknown as ChatDone),
      onError: cb.onError,
    });
  }

  try {
    await withWakeRetry({
      attempt,
      classifyError: (err) =>
        err instanceof StreamRefusedError ? classifyWakeSignal(err.status, err.detail) : "none",
      // A chat question is never safely resent automatically once it may have reached the
      // LLM (double-answer/double-cost risk) -- only a PROVABLY-safe ("safe") failure loops.
      idempotent: false,
      onWaking: cb.onWaking,
      onRecovered: cb.onRecovered,
      budgetMs: wakeTuning.budgetMs,
      now: wakeTuning.now,
      sleep: wakeTuning.sleep,
    });
  } catch (err) {
    if (err instanceof WakeUpError) {
      cb.onWakeFailed?.();
      return;
    }
    if (err instanceof StreamRefusedError) {
      // DA-G2-7: a 404 drops the stale conversation id (the next message starts a new one).
      const refused = refusedStream(
        err.status ?? 0,
        err.detail ?? "Something went wrong.",
        body.conversation_id ?? null,
      );
      cb.onError?.(refused.detail, refused.conversationId, refused.dropConversation);
      return;
    }
    throw err; // a genuine programming error -- never swallowed silently
  }
}
