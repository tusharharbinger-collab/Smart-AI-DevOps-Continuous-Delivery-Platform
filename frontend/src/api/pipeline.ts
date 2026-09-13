import { apiClient } from "./client";

export interface PipelineRun {
  pipeline_run_id: string;
  status: string;
  current_stage?: string;
  current_traffic_weight?: number;
  target_version?: string;
}

export const pausePipeline = (pipelineRunId: string) =>
  apiClient.post(`/api/v1/pipelines/${pipelineRunId}/pause`);

export const resumePipeline = (pipelineRunId: string) =>
  apiClient.post(`/api/v1/pipelines/${pipelineRunId}/resume`);

export const triggerRollback = (pipelineRunId: string) =>
  apiClient.post(`/api/v1/pipelines/${pipelineRunId}/rollback`);

export const getRun = (pipelineRunId: string) =>
  apiClient.get<PipelineRun>(`/api/v1/pipelines/runs/${pipelineRunId}`);

export const listPipelines = () =>
  apiClient.get<{ pipelines: { pipeline_id: string; name: string; created_at: string }[] }>(
    "/api/v1/pipelines"
  );
