// Typed client for the SecRAG backend API.

import { consumeChatStream, refusedStream } from "./chatStream";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const response = await fetch(`${API_URL}${path}`, { ...options, headers });
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

export function register(email: string, password: string): Promise<{ access_token: string }> {
  return request("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
}

export function login(email: string, password: string): Promise<{ access_token: string }> {
  return request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
}

export function me(token: string): Promise<UserInfo> {
  return request("/auth/me", {}, token);
}

export function resendVerification(
  token: string,
): Promise<{ detail: string; verification_link: string | null }> {
  return request("/auth/resend-verification", { method: "POST" }, token);
}

export function deleteAccount(token: string): Promise<{ detail: string }> {
  return request("/account", { method: "DELETE" }, token);
}

// --- conversations -------------------------------------------------------

export function listConversations(token: string): Promise<ConversationSummary[]> {
  return request("/conversations", {}, token);
}

export function getConversation(id: string, token: string): Promise<ConversationDetail> {
  return request(`/conversations/${id}`, {}, token);
}

export function renameConversation(
  id: string,
  title: string,
  token: string,
): Promise<{ detail: string }> {
  return request(`/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }, token);
}

export function deleteConversation(id: string, token: string): Promise<{ detail: string }> {
  return request(`/conversations/${id}`, { method: "DELETE" }, token);
}

// --- streaming chat (Server-Sent Events over fetch) ----------------------

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
  token: string,
  cb: StreamCallbacks,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
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
