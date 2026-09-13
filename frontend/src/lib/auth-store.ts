import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface Session {
  access_token: string;
  refresh_token: string;
  tenant_id: string;
  role: string;
  email: string;
}

interface AuthState {
  session: Session | null;
  setSession: (session: Session) => void;
  clearSession: () => void;
  updateTokens: (accessToken: string, refreshToken: string) => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      session: null,
      setSession: (session) => set({ session }),
      clearSession: () => set({ session: null }),
      // Phase 4 (security hardening): access tokens are short-lived (15 min)
      // and rotated together with the refresh token on every silent refresh
      // (api/client.ts) — this updates just the two token fields in place
      // without disturbing the rest of the session (tenant_id/role/email).
      updateTokens: (accessToken, refreshToken) =>
        set((state) =>
          state.session
            ? { session: { ...state.session, access_token: accessToken, refresh_token: refreshToken } }
            : state
        ),
    }),
    { name: "cd_platform_session" }
  )
);

/** Non-reactive read for use outside React components (e.g. the fetch wrapper). */
export function getSession(): Session | null {
  return useAuthStore.getState().session;
}

export function clearSession() {
  useAuthStore.getState().clearSession();
}

export function updateTokens(accessToken: string, refreshToken: string) {
  useAuthStore.getState().updateTokens(accessToken, refreshToken);
}
