/**
 * frontend/src/lib/guardrailSuggestions.ts
 *
 * "AI should generate needed things on the next step, based on this" (the
 * selected infra proposal) — the guardrail half of that request, alongside
 * _prompt_from_intent_spec threading the same infra_proposal into the
 * Pipeline Architect Agent's prompt (projects_router.py).
 *
 * Deliberately a deterministic function, not another LLM call: guardrail
 * VALUES (confidence floor, sample size, cost-delta ceiling) are config the
 * human explicitly reviews and can edit before creating the project — never
 * a verification decision itself (that stays exclusively the statistical
 * tests' job, per invariant 1: no static thresholds in the verdict path).
 * A human clicks "Apply suggested guardrails" to accept these as a
 * starting point; nothing here is auto-applied.
 */
import type { EnvironmentTier, IntentSpec } from "@/api/intentSpec";
import type { InfraProposal } from "@/api/infraDrafts";

export interface GuardrailSuggestion {
  confidence_floor: number;
  min_sample_size: number;
  max_cost_delta_percent: number;
  manual_approval_required: boolean;
  rationale: string[];
}

const TIER_BASE: Record<EnvironmentTier, Omit<GuardrailSuggestion, "rationale">> = {
  dev: { confidence_floor: 0.75, min_sample_size: 100, max_cost_delta_percent: 25, manual_approval_required: false },
  staging: { confidence_floor: 0.85, min_sample_size: 150, max_cost_delta_percent: 15, manual_approval_required: true },
  production: { confidence_floor: 0.9, min_sample_size: 200, max_cost_delta_percent: 10, manual_approval_required: true },
};

export function suggestGuardrails(spec: IntentSpec, infraProposal?: InfraProposal | null): GuardrailSuggestion {
  const base = TIER_BASE[spec.environment_tier];
  const suggestion: GuardrailSuggestion = { ...base, rationale: [`'${spec.environment_tier}' tier baseline.`] };

  if (spec.needs_database) {
    suggestion.confidence_floor = Math.max(suggestion.confidence_floor, 0.85);
    suggestion.min_sample_size = Math.max(suggestion.min_sample_size, 150);
    suggestion.rationale.push("Has a database dependency — treating regressions as higher-stakes raises the confidence floor and sample-size floor.");
  }

  if (spec.monthly_budget_usd != null) {
    suggestion.max_cost_delta_percent = Math.min(suggestion.max_cost_delta_percent, 10);
    suggestion.rationale.push(`A declared monthly budget ceiling ($${spec.monthly_budget_usd.toFixed(2)}) tightens the cost-delta guardrail.`);
  }

  const estimatedCost = infraProposal?.estimated_monthly_cost_usd;
  if (estimatedCost != null && spec.monthly_budget_usd != null && estimatedCost > spec.monthly_budget_usd * 0.8) {
    suggestion.max_cost_delta_percent = Math.min(suggestion.max_cost_delta_percent, 5);
    suggestion.manual_approval_required = true;
    suggestion.rationale.push(
      `The selected proposal's real AI cost estimate ($${estimatedCost.toFixed(2)}/mo) is already close to the budget ceiling — tightened further and requires manual approval.`
    );
  }

  return suggestion;
}
