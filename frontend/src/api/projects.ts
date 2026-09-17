/**
 * frontend/src/api/projects.ts
 *
 * Phase 8 (docs/roadmap/08-project-workspaces.md) — the project layer.
 *
 * A project wraps an existing pipeline: `pipeline_id` on a Project is what
 * lets the project workspace hand the EXISTING Pipeline View / Verification
 * Inspector / Policy & Gates / Audit Ledger screens their usual
 * `{pipelineId, pipelineRunId, tenantId}` context unchanged.
 */
import { apiClient } from "@/api/client";

export type ProjectStatus =
  | "IDLE"
  | "BUILDING"
  | "TESTING"
  | "VERIFYING"
  | "HEALTHY"
  | "ROLLED_BACK"
  | "FAILED";

export type RunStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "PAUSED"
  | "AWAITING_APPROVAL"
  | "ROLLED_BACK";

export type StageStatus = "PENDING" | "RUNNING" | "SUCCESS" | "FAILED" | string;

export interface ProjectSummary {
  project_id: string;
  pipeline_id: string | null;
  name: string;
  /** Null for a pipeline adopted from before projects existed (no repo behind it). */
  repo_url: string | null;
  branch: string;
  container_image: string | null;
  active_production_tag: string;
  canary_tag: string | null;
  status: ProjectStatus;
  created_at: string;
  latest_run_id: string | null;
  latest_run_status: RunStatus | null;
  latest_run_stage: string | null;
  latest_traffic_weight: number | null;
  latest_commit_sha: string | null;
  latest_started_at: string | null;
  latest_completed_at: string | null;
  latest_verdict: string | null;
  latest_confidence: number | null;
  runs_7d: number;
  /** null when no finished runs exist in the window — never a filler number. */
  success_rate_7d: number | null;
  mttv_seconds: number | null;
  /**
   * Real gap found live: this was computed at onboarding and thrown away —
   * never persisted, never returned, so there was no way to click a link
   * and check "is my product truly live" the way Render/Vercel do. Null
   * for a pre-existing project onboarded before this existed, or one with
   * no path_prefix at all (e.g. an adopted pre-Phase-8 pipeline).
   */
  live_url: string | null;
  /** "kubernetes" (default, local Kind cluster) or "aws_ecs" (real AWS Fargate). */
  deploy_target: "kubernetes" | "aws_ecs";
  /** "canary" (progressive, statistically verified) or "blue_green" (instant, health-gated). */
  deploy_mode: "canary" | "blue_green";
  /**
   * Set by pipeline-worker's shared/live_url_check.py after every real
   * cutover — a genuine HTTP GET through the real ALB/gateway, not just a
   * report that the traffic weight was flipped. Null for a project that
   * predates this, or whose deploy path doesn't run the check yet.
   */
  live_url_status: "verified" | "failed" | null;
  live_url_verified_at: string | null;
}

export interface ProjectDetail {
  project_id: string;
  pipeline_id: string | null;
  name: string;
  repo_url: string | null;
  branch: string;
  root_directory: string;
  dockerfile_path: string;
  test_command: string | null;
  container_image: string | null;
  active_production_tag: string;
  canary_tag: string | null;
  status: ProjectStatus;
  created_at: string;
  latest_run: ProjectRun | null;
  live_url: string | null;
  deploy_target: "kubernetes" | "aws_ecs";
  deploy_mode: "canary" | "blue_green";
  live_url_status: "verified" | "failed" | null;
  live_url_verified_at: string | null;
}

export interface ProjectRun {
  pipeline_run_id: string;
  target_version?: string;
  status: RunStatus;
  current_stage: string | null;
  current_traffic_weight: number | null;
  trigger_type: string | null;
  commit_sha: string | null;
  commit_message: string | null;
  started_at: string;
  completed_at: string | null;
  verdict?: string | null;
  confidence?: number | null;
}

export interface RunStages {
  run_id: string;
  project_id: string;
  run_status: RunStatus;
  current_stage: string | null;
  current_traffic_weight: number | null;
  stages: { name: string; status: StageStatus }[];
}

export interface CreateProjectInput {
  name: string;
  /** "repository" (clone + build + test + canary) or "existing_image" (deploy a pre-built image straight into the canary loop). */
  source_type: "repository" | "existing_image";
  /** Required when source_type is "repository"; null for "existing_image". */
  repo_url: string | null;
  repo_private: boolean;
  branch: string;
  root_directory: string;
  /** Set when building from a Dockerfile; null when using language/start_command synthesis instead. */
  dockerfile_path: string | null;
  /** Populated only when dockerfile_path is null — see shared/repo_scanner.py's BuildDetection. */
  language: string | null;
  /** Only meaningful when language === "node" ("spa" | "nextjs" | "node-server"). */
  framework: string | null;
  start_command: string | null;
  manifest_path: string | null;
  test_command: string | null;
  container_image: string;
  active_production_tag: string;
  canary_tag: string;
  /** An id from listRegistryCredentials(); only meaningful for "existing_image". */
  registry_credential_id: string | null;
  /** Networking for the generated Deployment/Service/HTTPRoute. */
  port: number;
  health_check_path: string;
  /** Null falls back to `/api/v1/{name}` server-side. */
  path_prefix: string | null;
  guardrails: {
    confidence_floor: number;
    min_sample_size: number;
    max_cost_delta_percent: number;
  };
  /**
   * Real gap found live: freeze windows and approver roles used to be
   * hardcoded server-side for every project — never actually wizard-
   * configurable. `deploy_mode` "blue_green" is only accepted when
   * `deploy_target` is "aws_ecs" — the server rejects blue_green +
   * kubernetes with a 422 (see projects_router.py's create_project).
   */
  deploy_policy: {
    deploy_mode: "canary" | "blue_green";
    blocked_deploy_windows: { days: string[]; start_time: string; end_time: string }[];
    manual_approval_required: boolean;
    manual_approval_roles: string[];
  };
  traffic_steps: number[];
  provision_cluster: boolean;
  /**
   * Module 8 — real second deployment target. "kubernetes" (default) uses
   * the local Kind cluster + Envoy Gateway; "aws_ecs" provisions this
   * project onto the shared real AWS ECS Fargate cluster + ALB instead
   * (see docs/roadmap/09-universal-delivery-platform.md). Only meaningful
   * when provision_cluster is true.
   */
  deploy_target: "kubernetes" | "aws_ecs";
  aws_region: string;
}

export interface CreateProjectResult {
  project_id: string;
  pipeline_id: string;
  name: string;
  namespace: string;
  generated_pipeline_yaml: string;
  cluster_provisioning: { attempted: boolean; succeeded: boolean; detail: string | null };
  live_url: string | null;
}

export const listProjects = () =>
  apiClient.get<{ projects: ProjectSummary[] }>("/api/v1/projects");

export const getProject = (projectId: string) =>
  apiClient.get<ProjectDetail>(`/api/v1/projects/${projectId}`);

export const createProject = (input: CreateProjectInput) =>
  apiClient.post<CreateProjectResult>("/api/v1/projects", input);

export const deleteProject = (projectId: string) =>
  apiClient.del<{
    deleted: string;
    pipeline_retained: boolean;
    /**
     * Real gap found live: deleting a project used to only remove the DB
     * row — the real Deployments/Services/HTTPRoute it onboarded were left
     * running in the cluster forever. Best-effort, mirroring create's own
     * cluster_provisioning shape.
     */
    cluster_deprovisioning: { attempted: boolean; succeeded: boolean; detail: string | null };
  }>(`/api/v1/projects/${projectId}`);

export const listProjectRuns = (projectId: string) =>
  apiClient.get<{ runs: ProjectRun[] }>(`/api/v1/projects/${projectId}/runs`);

export const getRunStages = (projectId: string, runId: string) =>
  apiClient.get<RunStages>(`/api/v1/projects/${projectId}/runs/${runId}/stages`);

export const triggerRollout = (
  projectId: string,
  body: { commit_sha?: string; commit_message?: string; target_version?: string } = {}
) =>
  apiClient.post<{ pipeline_run_id: string; project_id: string; status: string }>(
    `/api/v1/projects/${projectId}/rollout`,
    { ...body, trigger_type: "MANUAL_UI" }
  );

export const rollbackRun = (projectId: string, runId: string) =>
  apiClient.post<{ status: string }>(`/api/v1/projects/${projectId}/runs/${runId}/rollback`);

export interface RolloutState {
  has_rollout_state: boolean;
  status?: string;
  current_step_index?: number;
  total_steps?: number;
  current_step_weight?: number | null;
  awaiting_approval?: boolean;
}

export const getRunRolloutState = (projectId: string, runId: string) =>
  apiClient.get<RolloutState>(`/api/v1/projects/${projectId}/runs/${runId}/rollout`);

export const approveRun = (projectId: string, runId: string) =>
  apiClient.post<{ status: string; canary_weight?: number; reasons?: string[] }>(
    `/api/v1/projects/${projectId}/runs/${runId}/approve`
  );

export interface GeneratePipelineResult {
  pipeline_yaml: string;
  summary_of_changes: string;
  valid: boolean;
}

/**
 * Natural-language pipeline authoring — returns a CANDIDATE YAML only.
 * Never auto-saved: the caller must still populate the Policy & Gates
 * editor and go through the existing savePolicy() flow for a human to
 * actually commit it.
 */
export const generatePipelineFromPrompt = (projectId: string, prompt: string) =>
  apiClient.post<GeneratePipelineResult>(`/api/v1/projects/${projectId}/pipeline/generate`, { prompt });

export interface StageFailureAnalysis {
  likely_cause: string;
  evidence: string[];
  suggested_fix: string;
}

export const getRunFailureAnalysis = (projectId: string, runId: string) =>
  apiClient.get<{ failure_analysis: StageFailureAnalysis | null }>(
    `/api/v1/projects/${projectId}/runs/${runId}/failure-analysis`
  );

/**
 * Build-only dry run (services/pipeline-worker/src/build_preview.py) — runs
 * BEFORE a project exists, so it lives at /projects/build-preview rather
 * than nested under a project id. Clone -> build (real Dockerfile or
 * synthesized) -> test, zero deploy/cluster involvement. The same
 * run_build_task/run_test_task a real pipeline's build/test stages call,
 * so "does this build" here and during an actual rollout can never drift
 * apart into two different answers.
 */
export interface BuildPreviewRequest {
  repo_url: string;
  ref: string;
  repo_private: boolean;
  root_directory: string;
  dockerfile_path: string | null;
  language: string | null;
  framework: string | null;
  manifest_path: string | null;
  start_command: string | null;
  test_command: string | null;
}

export const startBuildPreview = (body: BuildPreviewRequest) =>
  apiClient.post<{ run_id: string; status: string }>("/api/v1/projects/build-preview", body);

export const getBuildPreviewLogs = (runId: string) =>
  apiClient.get<{ lines: string[] }>(`/api/v1/projects/build-preview/${runId}/logs`);

export interface BuildPreviewResult {
  status: "running" | "succeeded" | "failed";
  updated_at: string;
  stage?: string;
  error?: string;
  /** true = a repo/config problem the human must fix; false = a platform-side outage. */
  human_side?: boolean;
  dockerfile_path?: string;
  /** Test command failed but never blocks the build — see build_preview.py. Present only when a test command was set and it failed. */
  test_warning?: string | null;
}

export const getBuildPreviewResult = (runId: string) =>
  apiClient.get<BuildPreviewResult>(`/api/v1/projects/build-preview/${runId}/result`);
