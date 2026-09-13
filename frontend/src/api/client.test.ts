import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, ApiError } from "./client";
import { useAuthStore } from "@/lib/auth-store";

describe("apiClient", () => {
  beforeEach(() => {
    useAuthStore.getState().clearSession();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends no Authorization header when there's no session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiClient.get("/api/v1/pipelines");

    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it("sends a Bearer token once a session exists", async () => {
    useAuthStore.getState().setSession({
      access_token: "abc123",
      refresh_token: "refresh123",
      tenant_id: "t1",
      role: "developer",
      email: "a@b.com",
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiClient.get("/api/v1/pipelines");

    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer abc123");
  });

  it("throws ApiError with the response status on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("nope", { status: 500 }))
    );

    await expect(apiClient.get("/api/v1/pipelines")).rejects.toMatchObject({ status: 500 } satisfies Partial<ApiError>);
  });

  it("on a 401, silently refreshes the access token and retries the original request once", async () => {
    useAuthStore.getState().setSession({
      access_token: "expired-token",
      refresh_token: "valid-refresh",
      tenant_id: "t1",
      role: "developer",
      email: "a@b.com",
    });

    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.endsWith("/api/v1/pipelines") && fetchMock.mock.calls.length === 1) {
        return new Response("unauthorized", { status: 401 });
      }
      if (url.endsWith("/api/v1/auth/refresh")) {
        return new Response(
          JSON.stringify({ access_token: "new-token", refresh_token: "new-refresh" }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await apiClient.get("/api/v1/pipelines");

    expect(result).toEqual({ ok: true });
    expect(useAuthStore.getState().session?.access_token).toBe("new-token");
    // login/retry/refresh = 3 calls: original (401) -> refresh -> retried original
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const retryHeaders = fetchMock.mock.calls[2][1]!.headers as Record<string, string>;
    expect(retryHeaders.Authorization).toBe("Bearer new-token");
  });

  it("clears the session when the refresh token itself is invalid", async () => {
    useAuthStore.getState().setSession({
      access_token: "expired-token",
      refresh_token: "dead-refresh",
      tenant_id: "t1",
      role: "developer",
      email: "a@b.com",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/api/v1/auth/refresh")) {
          return new Response("invalid", { status: 401 });
        }
        return new Response("unauthorized", { status: 401 });
      })
    );

    await expect(apiClient.get("/api/v1/pipelines")).rejects.toMatchObject({ status: 401 });
    expect(useAuthStore.getState().session).toBeNull();
  });
});
