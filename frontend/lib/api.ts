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

export function register(email: string, password: string): Promise<void> {
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

export function resendVerification(): Promise<{ detail: string }> {
  return request("/auth/resend-verification", { method: "POST" });
}

export function deleteAccount(): Promise<{ detail: string }> {
  return request("/account", { method: "DELETE" });
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
