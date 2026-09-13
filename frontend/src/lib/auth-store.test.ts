import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "./auth-store";

describe("auth-store", () => {
  beforeEach(() => {
    useAuthStore.getState().clearSession();
    localStorage.clear();
  });

  it("starts with no session", () => {
    expect(useAuthStore.getState().session).toBeNull();
  });

  it("setSession stores the session and persists it", () => {
    const session = { access_token: "tok", refresh_token: "reftok", tenant_id: "t1", role: "developer", email: "a@b.com" };
    useAuthStore.getState().setSession(session);
    expect(useAuthStore.getState().session).toEqual(session);
  });

  it("clearSession removes the session", () => {
    useAuthStore.getState().setSession({ access_token: "tok", refresh_token: "reftok", tenant_id: "t1", role: "developer", email: "a@b.com" });
    useAuthStore.getState().clearSession();
    expect(useAuthStore.getState().session).toBeNull();
  });

  it("updateTokens replaces only the token fields, keeping the rest of the session", () => {
    useAuthStore.getState().setSession({ access_token: "old", refresh_token: "old-ref", tenant_id: "t1", role: "developer", email: "a@b.com" });
    useAuthStore.getState().updateTokens("new", "new-ref");
    expect(useAuthStore.getState().session).toEqual({
      access_token: "new", refresh_token: "new-ref", tenant_id: "t1", role: "developer", email: "a@b.com",
    });
  });
});
