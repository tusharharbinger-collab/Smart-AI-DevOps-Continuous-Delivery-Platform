/**
 * frontend/src/api/client.ts
 *
 * Thin fetch wrapper for the api-gateway REST surface. Auth token comes from
 * the Zustand auth store (src/lib/auth-store.ts), persisted to localStorage
 * under one key, and sent as a Bearer token — the gateway verifies this
 * token's signature (HS256, JWT_SECRET_KEY) rather than trusting an
 * unverified claim.
 *
 * Phase 4 (security hardening): access tokens are now short-lived (15 min,
 * down from 8 hours) so a stolen one has a small blast radius. Without
 * this file doing something about it, that would mean the user gets
 * kicked to /login every 15 minutes — so a 401 first tries exactly ONE
 * silent refresh (via the longer-lived refresh token) and retries the
 * original request before giving up and forcing a real re-login.
 * `refreshPromise` dedupes concurrent 401s (several in-flight requests
 * expiring at once) into a single refresh call rather than a stampede.
 */
import { getSession, clearSession, updateTokens } from "@/lib/auth-store";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

function authHeaders(): HeadersInit {
  const session = getSession();
  return {
    "Content-Type": "application/json",
    ...(session ? { Authorization: `Bearer ${session.access_token}` } : {}),
  };
}

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

let refreshPromise: Promise<boolean> | null = null;

/** Returns true if a new access token was obtained, false if the refresh token itself is invalid/expired. */
async function refreshAccessToken(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      const session = getSession();
      if (!session) return false;
      try {
        const res = await fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: session.refresh_token }),
        });
        if (!res.ok) return false;
        const body = (await res.json()) as { access_token: string; refresh_token: string };
        updateTokens(body.access_token, body.refresh_token);
        return true;
      } catch {
        return false;
      }
    })().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

async function request<T>(path: string, options: RequestInit = {}, isRetry = false): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers ?? {}) },
  });
  if (res.status === 401) {
    if (!isRetry && (await refreshAccessToken())) {
      return request<T>(path, options, true);
    }
    // Either this was already a retry, or the refresh token itself is
    // invalid/expired — there is no valid session left to recover, so
    // force a fresh login rather than let every subsequent call fail the
    // same way silently.
    clearSession();
    window.location.href = "/login";
    throw new ApiError("Session expired — please log in again.", 401);
  }
  if (!res.ok) {
    const body = await res.text();
    throw new ApiError(`API ${options.method ?? "GET"} ${path} failed: ${res.status} ${body}`, res.status);
  }
  const contentType = res.headers.get("content-type") ?? "";
  return (contentType.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const apiClient = {
  get: <T>(path: string, headers?: HeadersInit) => request<T>(path, { headers }),
  post: <T>(path: string, body?: unknown, headers?: HeadersInit) =>
    request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined, headers }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

export { API_BASE_URL, authHeaders, ApiError };
