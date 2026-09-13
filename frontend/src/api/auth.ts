import { API_BASE_URL, ApiError } from "./client";
import { useAuthStore, getSession, clearSession, type Session } from "@/lib/auth-store";

export async function login(email: string, password: string): Promise<Session> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(body.detail ?? "Login failed", res.status);
  }
  const session = (await res.json()) as Session;
  useAuthStore.getState().setSession(session);
  return session;
}

/**
 * Revokes the refresh token server-side (Phase 4) before clearing local
 * session state — without this, "logging out" only forgot the token on
 * this device; the same refresh token would still silently mint new access
 * tokens for anyone who'd copied it. Best-effort: local logout proceeds
 * even if the network call fails, since staying logged in locally forever
 * because the revoke request happened to drop is a worse failure mode.
 */
export async function logout(): Promise<void> {
  const session = getSession();
  if (session) {
    try {
      await fetch(`${API_BASE_URL}/api/v1/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: session.refresh_token }),
      });
    } catch {
      // best-effort — see docstring
    }
  }
  clearSession();
}
