/**
 * frontend/src/api/reports.ts — Reports & Cost tabs (P0, 2026-09-16).
 *
 * Both backend endpoints already existed (Report 1 / Report 3) before this
 * file — the real gap was that zero frontend code referenced either one.
 * `getCostHistory` is the one genuinely new endpoint this work added
 * (projects_router.py::get_project_cost_history).
 */
import { apiClient } from "./client";

export interface AiDigestSummary {
  summary: string;
  notable_trend: "improving" | "stable" | "degrading";
  talking_points: string[];
}

export interface DeliveryHealthDigest {
  tenant_id: string;
  period_days: number;
  generated_at: string;
  total_deployments: number;
  rollback_count: number;
  rollback_rate_percent: number;
  mean_time_to_verify_seconds: number | null;
  pipeline_success_rate_percent: number;
  avg_cost_delta_percent: number;
  ai_summary: AiDigestSummary | null;
}

export const getDeliveryHealthDigest = (tenantId: string, days = 7) =>
  apiClient.get<DeliveryHealthDigest>(`/api/v1/reports/digest/${tenantId}?days=${days}`);

export interface MetricEvidenceEntry {
  metric_name?: string;
  citation?: string;
  [key: string]: unknown;
}

export interface DeploymentReport {
  report_id: string;
  pipeline_run_id: string;
  final_verdict: string;
  confidence: number;
  composite_score: number;
  metric_evidence: Record<string, MetricEvidenceEntry> | null;
  tier1_breaches: string[] | null;
  rca_summary: string | null;
  cost_analysis: {
    baseline_cost: number;
    canary_cost: number;
    delta_percent: number;
    rightsizing_rec: RightsizingRecommendation | null;
  } | null;
  timestamp_utc: string;
}

export const getDeploymentReport = (runId: string) =>
  apiClient.get<DeploymentReport>(`/api/v1/reports/deployment/${runId}`);

export interface RightsizingRecommendation {
  efficiency_cpu: number;
  efficiency_mem: number;
  is_overprovisioned: boolean;
  recommended_cpu_vcpu: number;
  recommended_mem_gib: number;
  action: string;
  requires_approval_role: string;
}

export interface CostHistoryEntry {
  cost_id: string;
  pipeline_run_id: string;
  baseline_cost: number;
  canary_cost: number;
  delta_percent: number;
  rightsizing_rec: RightsizingRecommendation | null;
  computed_at: string;
  trigger_type: string | null;
  commit_sha: string | null;
  commit_message: string | null;
  performance_correlation?: {
    metric_name: string;
    latency_delta_percent: number;
  } | null;
}

export interface CostHistoryResponse {
  cost_history: CostHistoryEntry[];
  total_baseline_cost: number;
  total_canary_cost: number;
}

export const getCostHistory = (projectId: string, limit = 50) =>
  apiClient.get<CostHistoryResponse>(`/api/v1/projects/${projectId}/cost-history?limit=${limit}`);
