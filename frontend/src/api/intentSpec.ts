/**
 * frontend/src/api/intentSpec.ts
 *
 * The Requirements Form's data shape — mirrors shared/intent_spec.py's
 * IntentSpec pydantic model field-for-field on purpose, so the two never
 * drift. This is the object the (future, Phase 4) Infra Architect Agent
 * will parameterize a golden-path archetype from — the human declares it
 * here, the LLM never guesses any field this shape already holds.
 */
import { apiClient } from "@/api/client";

export type EnvironmentTier = "dev" | "staging" | "production";
export type DatabaseType = "postgres" | "mysql";

export interface IntentSpec {
  environment_tier: EnvironmentTier;
  expected_rps: number | null;
  expected_concurrent_users: number | null;

  fargate_cpu: number | null;
  fargate_memory_mib: number | null;
  min_instances: number | null;
  max_instances: number | null;

  needs_database: boolean;
  database_type: DatabaseType | null;
  multi_az: boolean | null;
  needs_cache: boolean;
  needs_object_storage: boolean;

  public_facing: boolean;
  private_subnet_only: boolean;
  encryption_at_rest_required: boolean;

  monthly_budget_usd: number | null;
  aws_region: string;

  archetype: string | null;
  detected_database_hint: string | null;
  detected_cache_hint: string | null;
  detected_storage_hint: string | null;
  detected_confidence: string | null;
}

/** Phase 3 (§2.4) — the tiered outcome returned alongside a normalized spec. */
export interface DeploymentReadiness {
  outcome: "auto_advance" | "warn" | "require_approval";
  reasons: string[];
}

export interface NormalizedIntentSpec extends IntentSpec {
  readiness: DeploymentReadiness;
}

export interface FargateTierDefaults {
  cpu: number;
  memory_mib: number;
  min_instances: number;
  max_instances: number;
  multi_az: boolean;
}

export function emptyIntentSpec(tier: EnvironmentTier = "dev"): IntentSpec {
  return {
    environment_tier: tier,
    expected_rps: null,
    expected_concurrent_users: null,
    fargate_cpu: null,
    fargate_memory_mib: null,
    min_instances: null,
    max_instances: null,
    needs_database: false,
    database_type: null,
    multi_az: null,
    needs_cache: false,
    needs_object_storage: false,
    public_facing: true,
    private_subnet_only: false,
    encryption_at_rest_required: false,
    monthly_budget_usd: null,
    aws_region: "us-east-1",
    archetype: null,
    detected_database_hint: null,
    detected_cache_hint: null,
    detected_storage_hint: null,
    detected_confidence: null,
  };
}

export const getIntentSpecTierDefaults = (tier: EnvironmentTier) =>
  apiClient.get<FargateTierDefaults>(`/api/v1/projects/intent-spec/tier-defaults/${tier}`);

/**
 * Validates + fills any unset ("use the tier default") field, and
 * evaluates Phase 3's tiered deployment-readiness gate (§2.4) on the
 * result. Pure computation server-side, no persistence yet.
 */
export const normalizeIntentSpec = (spec: IntentSpec) =>
  apiClient.post<NormalizedIntentSpec>(`/api/v1/projects/intent-spec/normalize`, spec);
