"use client";

const TOKEN_KEY = "agent_rag_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (typeof window === "undefined") return;
  if (token) {
    window.localStorage.setItem(TOKEN_KEY, token);
  } else {
    window.localStorage.removeItem(TOKEN_KEY);
  }
  window.dispatchEvent(new CustomEvent("agent-rag:auth-changed"));
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (
    init.body &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  const url = input.startsWith("http") ? input : `/api${input}`;
  const resp = await fetch(url, { ...init, headers });
  if (resp.status === 401 && typeof window !== "undefined") {
    setToken(null);
    if (window.location.pathname !== "/login" && window.location.pathname !== "/register") {
      window.location.href = "/login";
    }
  }
  return resp;
}

export async function apiJson<T = unknown>(
  input: string,
  init: RequestInit = {},
): Promise<T> {
  const resp = await apiFetch(input, init);
  const text = await resp.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!resp.ok) {
    const detailMessage =
      (data && typeof data === "object" && "detail" in (data as Record<string, unknown>)
        ? (data as { detail?: unknown }).detail
        : null) ?? resp.statusText;
    throw new ApiError(
      typeof detailMessage === "string" ? detailMessage : `HTTP ${resp.status}`,
      resp.status,
      data,
    );
  }
  return data as T;
}
