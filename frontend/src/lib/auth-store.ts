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

const PERSIST_KEY = "cd_platform_session";

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
    { name: PERSIST_KEY }
  )
);

// Real bug found live: a refresh token is single-use — api/client.ts's
// silent-refresh flow deletes the old one in Redis the instant a new one
// is issued. This store only ever wrote its refreshed tokens to ITS OWN
// tab's localStorage; a second open tab kept the now-rotated-away refresh
// token in memory and had no idea it had gone stale. The next time that
// second tab's access token expired, its silent refresh attempt used a
// token Redis no longer recognized, failed, and force-logged-out a session
// that was actually fine — reported live as "click New Service, it loads,
// then bounces to login." Zustand's `persist` writes state changes out to
// localStorage automatically, but does NOT listen for OTHER tabs writing
// to that same key by default; this re-hydrates from localStorage whenever
// a `storage` event fires for it, so every open tab picks up whichever
// tab most recently rotated the tokens instead of racing on a stale copy.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === PERSIST_KEY) {
      useAuthStore.persist.rehydrate();
    }
  });
}

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
