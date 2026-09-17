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

/**
 * shared/repo_scanner.py's deterministic detection (file-signature
 * matching, no AI, no guessing) — "method" is "yaml_manifest" | "dockerfile"
 * | "synthesized" | "unsupported". "confidence" drives whether the wizard
 * pre-checks a field or forces an explicit edit card.
 */
export interface BuildDetection {
  method: "yaml_manifest" | "dockerfile" | "synthesized" | "unsupported";
  dockerfile_path: string | null;
  language: string | null;
  /** Only ever set when language === "node" ("spa" | "nextjs" | "node-server"). */
  framework: string | null;
  manifest_path: string | null;
  start_command: string | null;
  test_command: string | null;
  test_config_found: boolean;
  confidence: "high" | "low";
  issues: string[];
  deploy_config: Record<string, unknown> | null;
  truncated: boolean;
  /**
   * Guaranteed Live Web App CI/CD — a suggestion only, never silently
   * applied: an ECS target group health check hits this path directly
   * (bypassing the ALB's path-prefix routing entirely), and almost no
   * arbitrary web app implements the flat "/healthz" this used to always
   * default to. "/" + 80 for a static site or Vite/CRA SPA, "/" + 3000
   * for Next.js, "/healthz" + 8080 otherwise (today's original default).
   */
  suggested_health_check_path: string;
  suggested_port: number;
}

export const getBuildDetection = (owner: string, repo: string, ref: string, token?: string) =>
  apiClient.get<BuildDetection>(
    `/api/v1/integrations/github/repos/${owner}/${repo}/build-detection?ref=${encodeURIComponent(ref)}`,
    tokenHeader(token)
  );

export interface RepoReport {
  features: {
    file_count: number;
    max_depth: number;
    has_tests: boolean;
    has_dockerfile: boolean;
    has_lockfile: boolean;
    has_ci_config: boolean;
    has_readme: boolean;
    has_license: boolean;
    dependency_count: number;
    language: string | null;
    framework: string | null;
    build_confidence: string;
  };
  risk: {
    risk_score: number;
    risk_level: "low" | "medium" | "high";
    risk_flags: string[];
  };
  cost: {
    task_cpu_units: number;
    task_memory_mib: number;
    task_cpu_vcpu: number;
    task_memory_gib: number;
    task_hourly_usd: number;
    steady_state_monthly_usd: number;
    estimated_rollout_window_usd: number;
    canary_step_hours: number;
    assumed_replicas: number;
  };
  narrative: string;
  truncated: boolean;
}

export const getRepoReport = (owner: string, repo: string, ref: string, token?: string) =>
  apiClient.get<RepoReport>(
    `/api/v1/integrations/github/repos/${owner}/${repo}/repo-report?ref=${encodeURIComponent(ref)}`,
    tokenHeader(token)
  );

/** Splits "https://github.com/acme/checkout" → { owner: "acme", repo: "checkout" }. */
export function parseRepoUrl(url: string): { owner: string; repo: string } | null {
  const match = url.match(/github\.com[/:]([^/]+)\/([^/.]+)/i);
  return match ? { owner: match[1], repo: match[2] } : null;
}

