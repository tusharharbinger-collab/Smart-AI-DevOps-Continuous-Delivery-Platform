/**
 * frontend/src/api/pipelinePreview.ts
 *
 * Phase 6 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5) — the Pipeline Architect
 * Agent's frontend client, wired for real: previewPipelineTemplate returns
 * the exact deterministic YAML generate_project_pipeline_yaml() would
 * produce (no DB write), and generateAiTunedPipeline calls the same real
 * Groq pipeline_generator.py + pipeline-worker validator retry loop the
 * existing post-creation pipeline editor already uses.
 */
import { apiClient } from "@/api/client";
import type { CreateProjectInput } from "@/api/projects";
import type { IntentSpec } from "@/api/intentSpec";
import type { InfraProposal } from "@/api/infraDrafts";

export const previewPipelineTemplate = (payload: CreateProjectInput) =>
  apiClient.post<{ policy_yaml: string }>(`/api/v1/projects/pipeline-preview/template`, payload);

export interface AiTunedPipelineResult {
  pipeline_yaml: string;
  summary_of_changes: string;
  valid: boolean;
}

/**
 * `infraProposal`, when the human has selected one (via RequirementsForm's
 * "Use this infrastructure"), is threaded into the tuning prompt so the
 * pipeline's guardrails are sized against the SPECIFIC resources/cost that
 * will actually be provisioned rather than just the IntentSpec's boolean
 * needs_database/needs_cache flags — see _prompt_from_intent_spec's
 * docstring in projects_router.py.
 */
export const generateAiTunedPipeline = (baseYaml: string, intentSpec: IntentSpec, infraProposal?: InfraProposal | null) =>
  apiClient.post<AiTunedPipelineResult>(`/api/v1/projects/pipeline-preview/generate`, {
    base_yaml: baseYaml,
    intent_spec: intentSpec,
    infra_proposal: infraProposal ?? null,
  });
