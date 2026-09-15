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
  dockerfile_path: string;
  test_command: string;
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
  traffic_steps: number[];
  provision_cluster: boolean;
}

export interface CreateProjectResult {
  project_id: string;
  pipeline_id: string;
  name: string;
  namespace: string;
  generated_pipeline_yaml: string;
  cluster_provisioning: { attempted: boolean; succeeded: boolean; detail: string | null };
}

export const listProjects = () =>
  apiClient.get<{ projects: ProjectSummary[] }>("/api/v1/projects");

export const getProject = (projectId: string) =>
  apiClient.get<ProjectDetail>(`/api/v1/projects/${projectId}`);

export const createProject = (input: CreateProjectInput) =>
  apiClient.post<CreateProjectResult>("/api/v1/projects", input);

export const deleteProject = (projectId: string) =>
  apiClient.del<{ deleted: string }>(`/api/v1/projects/${projectId}`);

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
