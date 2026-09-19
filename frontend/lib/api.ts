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

export function chat(question: string, token?: string): Promise<ChatResponse> {
  return request("/chat", { method: "POST", body: JSON.stringify({ question }) }, token);
}

export function deleteAccount(token: string): Promise<{ detail: string }> {
  return request("/account", { method: "DELETE" }, token);
}
