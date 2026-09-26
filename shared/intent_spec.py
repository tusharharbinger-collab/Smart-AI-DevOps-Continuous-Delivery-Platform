"""
shared/intent_spec.py

The IntentSpec: the structured object the Requirements Form (see
AI_AGENTIC_ORCHESTRATION_PLAN.md §2.1/§2.5) locks in, and the exact prompt
input the (future, Phase 4) Infra Architect Agent will parameterize a
golden-path archetype from. Deliberately shared (not api-gateway-only) so
explainability-service can import the identical shape later without a
second, drifting definition.

Design rule this file exists to enforce (Ground Rule 0a in the orchestration
plan): the human declares intent via this model's fields — the LLM is never
asked to guess anything this model can already hold. Every "unsure" case
(sizing, instance count) has a locked, non-guessed default sourced from
AWS's own fixed Fargate CPU/memory combinations (§R.8's research finding),
not an invented number — see FARGATE_TIER_DEFAULTS below.
"""
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class EnvironmentTier(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class DatabaseType(str, Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"


# Locked decision (AI_AGENTIC_ORCHESTRATION_PLAN.md §R.8 / §5A): AWS Fargate
# CPU/memory is a small, fixed set of supported combinations, not a
# continuous range — a team under 5 people / dev-staging workload doesn't
# need per-vCPU-cost tuning, per AWS's own Fargate guidance. These are
# defaults ONLY: the Requirements Form always shows them as editable, never
# locked, matching this module's "never guess beyond what the human
# confirmed" rule.
FARGATE_TIER_DEFAULTS: dict[EnvironmentTier, dict] = {
    EnvironmentTier.DEV: {"cpu": 256, "memory_mib": 512, "min_instances": 1, "max_instances": 1, "multi_az": False},
    EnvironmentTier.STAGING: {"cpu": 512, "memory_mib": 1024, "min_instances": 1, "max_instances": 2, "multi_az": False},
    # Smallest combo that supports Multi-AZ + min 2 tasks, per §R.8.
    EnvironmentTier.PRODUCTION: {"cpu": 1024, "memory_mib": 2048, "min_instances": 2, "max_instances": 4, "multi_az": True},
}


class IntentSpec(BaseModel):
    # ── Scale & traffic ──
    environment_tier: EnvironmentTier
    expected_rps: int | None = Field(default=None, ge=0)
    expected_concurrent_users: int | None = Field(default=None, ge=0)

    # ── Compute ── (None means "use the tier default" — see apply_tier_defaults)
    fargate_cpu: int | None = None
    fargate_memory_mib: int | None = None
    min_instances: int | None = Field(default=None, ge=1)
    max_instances: int | None = Field(default=None, ge=1)

    # ── Data ──
    needs_database: bool = False
    database_type: DatabaseType | None = None
    multi_az: bool | None = None  # None means "use the tier default"
    needs_cache: bool = False
    needs_object_storage: bool = False

    # ── Networking & security ──
    public_facing: bool = True
    private_subnet_only: bool = False
    encryption_at_rest_required: bool = False

    # ── Budget ──
    monthly_budget_usd: float | None = Field(default=None, gt=0)

    # ── Region & account ──
    aws_region: str = "us-east-1"

    # ── Traceability back to Phase 1's detection (never re-derived, just carried) ──
    archetype: str | None = None
    detected_database_hint: str | None = None
    detected_cache_hint: str | None = None
    detected_storage_hint: str | None = None
    # BuildDetection.confidence ("high" | "low") — feeds Phase 3's tiered
    # deployment-readiness check (shared/deployment_readiness.py); a
    # low-confidence detection is exactly the case that should require a
    # human's eyes before proceeding.
    detected_confidence: str | None = None

    @model_validator(mode="after")
    def _validate_consistency(self) -> "IntentSpec":
        if self.max_instances is not None and self.min_instances is not None and self.max_instances < self.min_instances:
            raise ValueError("max_instances must be >= min_instances")
        if self.multi_az and not self.needs_database:
            raise ValueError("multi_az only applies to a database — set needs_database first")
        if self.database_type is not None and not self.needs_database:
            raise ValueError("database_type set but needs_database is False")
        return self


def apply_tier_defaults(spec: IntentSpec) -> IntentSpec:
    """
    Fills every "use the tier default" (None) field from
    FARGATE_TIER_DEFAULTS — never overwrites a value the human actually
    set. Called once, server-side, right before the spec is locked; the
    Requirements Form itself may also call the read-only defaults endpoint
    to pre-populate the UI, but this function is the single source of
    truth both paths rely on.
    """
    defaults = FARGATE_TIER_DEFAULTS[spec.environment_tier]
    data = spec.model_dump()
    if data["fargate_cpu"] is None:
        data["fargate_cpu"] = defaults["cpu"]
    if data["fargate_memory_mib"] is None:
        data["fargate_memory_mib"] = defaults["memory_mib"]
    if data["min_instances"] is None:
        data["min_instances"] = defaults["min_instances"]
    if data["max_instances"] is None:
        # A human-set min_instances can legitimately exceed this tier's
        # default max (e.g. dev tier defaults to max=1, but they set
        # min_instances=3) — defaulting max below that would produce an
        # invalid spec out of a value they never touched. Bump up to match
        # rather than silently rejecting a combination they didn't cause.
        data["max_instances"] = max(defaults["max_instances"], data["min_instances"])
    if data["multi_az"] is None:
        data["multi_az"] = defaults["multi_az"] if spec.needs_database else False
    return IntentSpec(**data)
