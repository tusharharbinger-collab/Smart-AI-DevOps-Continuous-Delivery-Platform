/**
 * frontend/src/api/github.ts
 *
 * Phase 8 — GitHub repo/branch ingestion for the project-creation wizard.
 *
 * The token is held only in memory for the duration of the wizard (see
 * NewProject.tsx) and sent per-request as `X-GitHub-Token`. It is never
 * written to localStorage and never persisted server-side — the gateway
 * reads it off the header, uses it for one call, and discards it.
 */
import { apiClient } from "@/api/client";

export interface GitHubStatus {
  connected: boolean;
  /** False when the server has no GITHUB_CLIENT_ID/SECRET configured. */
  oauth_configured: boolean;
  /** True when a shared server-side token exists as a fallback. */
  server_token_fallback: boolean;
  login: string | null;
  avatar_url: string | null;
  name: string | null;
}

export interface GitHubRepo {
  id: number;
  name: string;
  full_name: string;
  default_branch: string;
  private: boolean;
  clone_url: string;
  description: string | null;
  updated_at: string | null;
}

export interface GitHubBranch {
  name: string;
  commit_sha: string | null;
}

function tokenHeader(token?: string): HeadersInit | undefined {
  return token ? { "X-GitHub-Token": token } : undefined;
}

export const getGitHubStatus = () =>
  apiClient.get<GitHubStatus>("/api/v1/integrations/github/status");

export const getAuthorizeUrl = () =>
  apiClient.get<{ authorize_url: string }>("/api/v1/integrations/github/authorize-url");

export const disconnectGitHub = () =>
  apiClient.post<{ connected: boolean }>("/api/v1/integrations/github/disconnect");

export const listRepos = (token?: string, search?: string) =>
  apiClient.get<{ repos: GitHubRepo[] }>(
    `/api/v1/integrations/github/repos${search ? `?search=${encodeURIComponent(search)}` : ""}`,
    tokenHeader(token)
  );

export const listBranches = (owner: string, repo: string, token?: string) =>
  apiClient.get<{ branches: GitHubBranch[] }>(
    `/api/v1/integrations/github/repos/${owner}/${repo}/branches`,
    tokenHeader(token)
  );

export const getHeadCommit = (owner: string, repo: string, ref: string, token?: string) =>
  apiClient.get<{ sha: string; message: string; author: string; committed_at: string }>(
    `/api/v1/integrations/github/repos/${owner}/${repo}/commits/${ref}`,
    tokenHeader(token)
  );

/** Splits "https://github.com/acme/checkout" → { owner: "acme", repo: "checkout" }. */
export function parseRepoUrl(url: string): { owner: string; repo: string } | null {
  const match = url.match(/github\.com[/:]([^/]+)\/([^/.]+)/i);
  return match ? { owner: match[1], repo: match[2] } : null;
}
