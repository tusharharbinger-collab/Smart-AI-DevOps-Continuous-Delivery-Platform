import { apiClient } from "./client";

export interface ValidationResult {
  valid: boolean;
  errors?: string[];
  parsedGates?: {
    blockedDeployWindows?: { days: string[]; startTime: string; endTime: string }[];
    manualApprovalRequired?: { beforeStages: string[]; approverRoles: string[] };
  };
  parsedGuardrails?: {
    requireMinimumConfidence?: number;
    minSampleSize?: number;
    maxPermittedCostDeltaPercent?: number;
  };
  dryRunAllowAction?: boolean;
}

export const validatePolicy = (policyYaml: string) =>
  apiClient.post<ValidationResult>("/api/v1/policies/validate", { policy_yaml: policyYaml });

export const savePolicy = (pipelineId: string, policyYaml: string) =>
  apiClient.post("/api/v1/policies", { pipeline_id: pipelineId, policy_yaml: policyYaml });

export const getPolicy = (pipelineId: string) =>
  apiClient.get<{ pipeline_id: string; policy_yaml: string }>(`/api/v1/policies/${pipelineId}`);
