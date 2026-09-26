/**
 * frontend/src/api/infraDrafts.ts
 *
 * Phase 4/5 (AI_AGENTIC_ORCHESTRATION_PLAN.md) — the Infra Architect
 * Agent's frontend client. Mirrors the backend's InfraGenerationResult
 * (services/explainability-service/src/infra_generator.py) and
 * infra_build_state row shape (services/api-gateway/src/routers/
 * projects_router.py) field-for-field.
 */
import { apiClient } from "@/api/client";
import type { IntentSpec } from "@/api/intentSpec";

export interface TopologyNode {
  id: string;
  type: string;
  label: string;
}

export interface TopologyEdge {
  source: string;
  target: string;
}

export interface InfraTopology {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

export interface CostBreakdownItem {
  resource: string;
  monthly_usd: number;
}

export interface PolicyCheckResult {
  check: string;
  /** False only for a hard failure - a warning still "passes" (see status). */
  passed: boolean;
  detail: string;
  /** Present on OPA's independent verdict (absent on the model's own self-reported list). */
  status?: "pass" | "warn" | "fail";
}

/** policies/infra_guardrails.rego's verdict on the real CloudFormation template - not the AI grading itself. */
export interface PolicyFinding {
  rule: string;
  resource: string;
  severity: "deny" | "warn";
  message: string;
}

export interface PolicyEvaluation {
  engine: "opa";
  policy: string;
  /** False means a hard failure: approval is refused until the proposal is edited to pass. */
  allowed: boolean;
  deny: PolicyFinding[];
  warn: PolicyFinding[];
}

export interface InfraProposal {
  name: string;
  topology: InfraTopology;
  iac_terraform: string;
  estimated_monthly_cost_usd: number;
  cost_breakdown: CostBreakdownItem[];
  policy_checks: PolicyCheckResult[];
  /** The model's own (unverified) checks, kept for transparency - `policy_checks` is OPA's independent verdict. */
  ai_self_reported_policy_checks?: PolicyCheckResult[];
  policy_evaluation?: PolicyEvaluation;
  /** Where the cost figure came from. "aws-price-list" = computed from AWS's own prices; "ai_estimate_unverified" = the model's guess (pricing was unavailable). */
  cost_estimate?: CostEstimate;
  /** The model's own figure, kept for transparency when the computed one replaced it. */
  ai_estimated_monthly_cost_usd?: number | null;
}

export interface CostEstimate {
  source: "aws-price-list" | "ai_estimate_unverified";
  reason?: string;
  currency?: string;
  /** Resources excluded from the total - never assumed to be $0. */
  unpriced?: { resource: string; type: string; reason: string }[];
  no_fixed_cost?: string[];
  assumptions?: string[];
  errors?: number;
}

export type InfraDraftStatus =
  | "INTENT_LOCKED"
  | "INFRA_DRAFTING"
  | "INFRA_DRAFT_FAILED"
  | "INFRA_PENDING_APPROVAL"
  | "INFRA_APPROVED"
  | "INFRA_CHANGE_SET_CREATING"
  | "INFRA_CHANGE_SET_READY"
  | "INFRA_CHANGE_SET_FAILED"
  | "INFRA_EXECUTION_APPROVED"
  | "INFRA_PROVISIONING"
  | "INFRA_PROVISIONED"
  | "INFRA_PROVISIONING_FAILED";

/** Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — one real AWS resource change from a CloudFormation Change Set. */
export interface ResourceChange {
  action: "Add" | "Modify" | "Remove" | "Import";
  logical_id: string;
  resource_type: string;
}

export type InfraSource = "ai_created" | "existing";

/** One selectable AWS resource from GET /infra-drafts/discover-existing. */
export interface ExistingResourceCandidate {
  id: string;
  label: string;
  details: Record<string, unknown>;
}

/** slot ("database" | "cache" | "ecs_cluster" | "load_balancer") -> candidates, scoped to what the archetype needs. */
export type DiscoveredResources = Record<string, ExistingResourceCandidate[]>;

export interface InfraDraftOptions {
  source?: InfraSource;
  /** slot -> real AWS identifier; required when source is "existing". */
  existingSelection?: Record<string, string>;
  /** A VERIFIED tenant-owned AWS connection to build in; omitted/null means the platform's own account. */
  awsConnectionId?: string | null;
}

export interface InfraDraft {
  draft_id: string;
  project_id: string | null;
  status: InfraDraftStatus;
  intent_spec: IntentSpec;
  archetype: string | null;
  infra_proposal: InfraProposal | null;
  readiness_outcome: "auto_advance" | "warn" | "require_approval" | null;
  readiness_reasons: string[];
  error_message: string | null;
  cloud_provider: string;
  change_set_id: string | null;
  stack_name: string | null;
  stack_arn: string | null;
  change_set_changes: ResourceChange[];
  provisioning_error: string | null;
  provisioning_outputs: Record<string, string>;
  /** AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md: "ai_created" designs new resources, "existing" attaches ones already in the AWS account. */
  source: InfraSource;
  /** slot -> the human's pick, re-verified against AWS server-side (real configuration, never client-supplied). */
  existing_resources: Record<string, { id: string; details: Record<string, unknown> }>;
  /** Set on a prompt-edited draft: the draft it revised. Edits are new rows, never in-place overwrites. */
  parent_draft_id: string | null;
  /** The tenant AWS connection this draft provisions into; null = the platform's own account. */
  aws_connection_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Calls the real Groq-backed Infra Architect Agent (services/explainability-service) — never a mock/canned response. */
export const createInfraDraft = (spec: IntentSpec, options: InfraDraftOptions = {}) =>
  apiClient.post<InfraDraft>(`/api/v1/projects/infra-drafts`, {
    ...spec,
    source: options.source ?? "ai_created",
    existing_selection: options.existingSelection ?? {},
    aws_connection_id: options.awsConnectionId ?? null,
  });

/** Read-only picklist of AWS resources that already exist for this archetype's slots - no AI call, no writes. */
export const discoverExistingInfra = (archetype: string, region: string, connectionId?: string | null) =>
  apiClient.get<DiscoveredResources>(
    `/api/v1/projects/infra-drafts/discover-existing?archetype=${encodeURIComponent(archetype)}&region=${encodeURIComponent(region)}` +
      (connectionId ? `&connection_id=${encodeURIComponent(connectionId)}` : "")
  );

/**
 * Prompt-driven edit. Returns a NEW draft (parent_draft_id -> draftId) that re-enters the approval gate -
 * an edit is never auto-applied, and can never remove an imported (Retain) resource.
 */
export const editInfraDraft = (draftId: string, instruction: string) =>
  apiClient.post<InfraDraft>(`/api/v1/projects/infra-drafts/${draftId}/edit`, { instruction });

export const getInfraDraft = (draftId: string) => apiClient.get<InfraDraft>(`/api/v1/projects/infra-drafts/${draftId}`);

export const approveInfraDraft = (draftId: string) =>
  apiClient.post<InfraDraft>(`/api/v1/projects/infra-drafts/${draftId}/approve`);

/** Phase 7 — real, zero-risk AWS preview (CloudFormation create_change_set). Only valid from INFRA_APPROVED. */
export const createInfraChangeSet = (draftId: string) =>
  apiClient.post<InfraDraft>(`/api/v1/projects/infra-drafts/${draftId}/create-change-set`);

/** The second, distinct human approval — this is the call that actually creates real, billable AWS resources. */
export const executeInfraChangeSet = (draftId: string) =>
  apiClient.post<InfraDraft>(`/api/v1/projects/infra-drafts/${draftId}/execute`);

/** Poll after execute — CloudFormation provisioning is asynchronous and can take minutes. */
export const getInfraProvisioningStatus = (draftId: string) =>
  apiClient.get<InfraDraft>(`/api/v1/projects/infra-drafts/${draftId}/status`);

/**
 * n8n-style visualization support: reverse lookup by project_id (rather
 * than draft_id) for the persistent project view — the draft was created
 * before the project existed, so ProjectWorkspace/PipelineDashboard have no
 * other way to find which infra topology (if any) belongs to this project.
 * Returns null for a project created without ever going through the
 * Requirements Form / infra-draft flow — an expected case, not an error.
 */
export const getProjectInfraDraft = (projectId: string) =>
  apiClient.get<InfraDraft | null>(`/api/v1/projects/${projectId}/infra-draft`);

export interface InfraFailureAnalysis {
  likely_cause: string;
  evidence: string[];
  suggested_fix: string;
  /** Present only when the fix is a template change; safe to hand to "Edit with AI". */
  suggested_edit_instruction: string | null;
  category: "name_conflict" | "permissions" | "quota" | "invalid_configuration" | "dependency" | "unknown";
  /** "model" = AI explanation with verified evidence; "deterministic" = rule-based (AI unavailable). */
  source: "model" | "model_ungrounded" | "deterministic";
  phase: "change_set" | "stack";
  events_examined: number;
}

/** Read-only: explains a FAILED draft from the real CloudFormation events. Applies nothing. */
export const analyzeInfraFailure = (draftId: string) =>
  apiClient.post<InfraFailureAnalysis>(`/api/v1/projects/infra-drafts/${draftId}/failure-analysis`, {});
