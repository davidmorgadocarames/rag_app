// Typed client for the SecRAG backend API, called ONLY through the BFF (T12.1.7, P12-D1):
// relative /api/... paths (no build-time backend URL, no NEXT_PUBLIC_API_URL), same-origin
// cookies for auth (no token argument anywhere -- the browser attaches the HttpOnly session
// cookie automatically) and a CSRF header read from the readable, non-HttpOnly
// `__Host-secrag_csrf` cookie on every state-changing request.

import { consumeChatStream, refusedStream } from "./chatStream.ts";

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

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (CSRF_METHODS.has(method)) {
    const csrf = currentCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(`/api${path}`, {
    ...options,
    method,
    headers,
    credentials: "same-origin", // the BFF is always same-origin: send the session cookie
  });
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

// T12.4.4: a generic 202, no session/token any more -- the caller never learns whether the
// address was new or already registered (only the real owner, by email, does).
export function register(email: string, password: string): Promise<{ detail: string }> {
  return request("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
}

export function login(email: string, password: string): Promise<void> {
  return request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
}

export function logout(): Promise<{ detail: string }> {
  return request("/auth/logout", { method: "POST" });
}

export function me(): Promise<UserInfo> {
  return request("/auth/me");
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

export interface AdminLatencyResponse {
  stages: StageLatencyOut[];
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

export function adminFeatures(): Promise<AdminFeaturesResponse> {
  return request("/admin/features");
}

export function adminMetrics(): Promise<AdminMetricsResponse> {
  return request("/admin/metrics");
}

export function adminLatency(): Promise<AdminLatencyResponse> {
  return request("/admin/latency");
}

export function adminEval(): Promise<AdminEvalResponse> {
  return request("/admin/eval");
}

export function adminErasure(): Promise<AdminErasureResponse> {
  return request("/admin/erasure");
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
}

export interface DefenceRunsResponse {
  runs: DefenceRunOut[];
}

export function adminLabRun(): Promise<DefenceLabRunResponse> {
  return request("/admin/lab/run", { method: "POST" });
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
  onToken?: (text: string) => void;
  onDone?: (data: ChatDone) => void;
  // Always called when the turn fails, with the conversation id when known;
  // dropConversation: the conversation is gone (404) — the next message starts a new one.
  onError?: (detail: string, conversationId: string | null, dropConversation?: boolean) => void;
}

export async function streamChat(
  body: { question: string; conversation_id?: string; version?: string },
  cb: StreamCallbacks,
): Promise<void> {
  const csrf = currentCsrfToken();
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
    cb.onError?.("The server cannot be reached. Please try again.", body.conversation_id ?? null);
    return;
  }
  if (!response.ok || !response.body) {
    let detail = `request failed (${response.status})`;
    try {
      const err = await response.json();
      if (typeof err?.detail === "string") detail = err.detail;
    } catch {
      // keep default
    }
    // DA-G2-7: a 404 drops the stale conversation id (the next message starts a new one).
    const refused = refusedStream(response.status, detail, body.conversation_id ?? null);
    cb.onError?.(refused.detail, refused.conversationId, refused.dropConversation);
    return;
  }

  await consumeChatStream(response.body, {
    onConversation: cb.onConversation,
    onStage: cb.onStage,
    onToken: cb.onToken,
    onDone: (event) => cb.onDone?.(event as unknown as ChatDone),
    onError: cb.onError,
  });
}
