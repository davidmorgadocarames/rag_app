// Typed client for the SecRAG backend API.

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
}

export interface ConversationDetail {
  id: string;
  title: string;
  messages: StoredMessage[];
}

class ApiError extends Error {}

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
    throw new ApiError(detail);
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
  onStage?: (stage: string) => void;
  onToken?: (text: string) => void;
  onDone?: (data: ChatDone) => void;
  onError?: (detail: string) => void;
}

export async function streamChat(
  body: { question: string; conversation_id?: string; version?: string },
  token: string,
  cb: StreamCallbacks,
): Promise<void> {
  const response = await fetch(`${API_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    let detail = `request failed (${response.status})`;
    try {
      const err = await response.json();
      if (typeof err?.detail === "string") detail = err.detail;
    } catch {
      // keep default
    }
    cb.onError?.(detail);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const event = JSON.parse(line.slice(5).trim());
      if (event.type === "stage") cb.onStage?.(event.stage);
      else if (event.type === "token") cb.onToken?.(event.text);
      else if (event.type === "done") cb.onDone?.(event as ChatDone);
      else if (event.type === "error") cb.onError?.(event.detail);
    }
  }
}
