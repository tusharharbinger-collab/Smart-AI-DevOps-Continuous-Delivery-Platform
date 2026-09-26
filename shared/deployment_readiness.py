"""
shared/deployment_readiness.py

Phase 3 (AI_AGENTIC_ORCHESTRATION_PLAN.md §2.4) — tiered outcomes for the
project-onboarding/deploy decision, applied to the EXISTING deploy path
(no AI-generated infra content exists yet; Phase 4 adds that). This is the
pre-deploy analog to the rollout-time gate `policies/delivery_guardrails.rego`
already enforces for canary promotion/rollback — same tiered philosophy
(Spacelift/env0's approve/reject/warn/require-approval, §R.3), different
decision point and deliberately kept in plain Python rather than OPA: this
gate fires once, at project-creation time, before any pipeline_run_id or
verdict exists for OPA's existing input shape to evaluate against.

Three possible outcomes, never a binary pass/fail:
  - "auto_advance"    — no human click needed, the common case for a simple,
                         well-detected, low-committed-cost project.
  - "warn"             — proceeds, but the human sees WHY before confirming.
  - "require_approval" — a hard pause; an explicit acknowledgment is needed.

This is exactly the mechanism that makes "keep human in the loop where
required" a defined condition instead of a vague always-ask default.
"""
from dataclasses import dataclass, field

from shared.intent_spec import IntentSpec

# A "low pre-approved ceiling" (§2.4) — deliberately small: this is a
# self-declared budget PREFERENCE at onboarding time (Phase 4 hasn't built
# the real AI cost estimate yet), not a computed cost, so the bar for
# "low enough to skip a click" is conservative on purpose.
AUTO_ADVANCE_COST_CEILING_USD = 50.0

# Matches shared/repo_scanner.py's ARCHETYPE_MULTI_SERVICE / _BACKGROUND_WORKER
# constants by value (not imported directly — repo_scanner has no Python
# dependency on this module and shouldn't gain one just for these two
# string literals).
_ARCHETYPE_MULTI_SERVICE = "multi_service"
_ARCHETYPE_BACKGROUND_WORKER = "background_worker"

OUTCOME_AUTO_ADVANCE = "auto_advance"
OUTCOME_WARN = "warn"
OUTCOME_REQUIRE_APPROVAL = "require_approval"


@dataclass
class DeploymentReadiness:
    outcome: str  # one of the OUTCOME_* constants above
    reasons: list[str] = field(default_factory=list)


def evaluate_deployment_readiness(spec: IntentSpec) -> DeploymentReadiness:
    """
    Precedence, highest first — a single reason at the "require_approval"
    tier is enough to stop there; lower tiers accumulate every reason that
    applies so the human sees the full picture, not just the first hit.
    """
    if spec.detected_confidence is not None and spec.detected_confidence != "high":
        return DeploymentReadiness(
            outcome=OUTCOME_REQUIRE_APPROVAL,
            reasons=[
                "Repo build detection was low-confidence — a real signal (e.g. a start "
                "command or build script) was missing, so the build path this project "
                "will actually use has not been confirmed."
            ],
        )
    if spec.archetype == _ARCHETYPE_MULTI_SERVICE:
        return DeploymentReadiness(
            outcome=OUTCOME_REQUIRE_APPROVAL,
            reasons=[
                "Matched the multi_service archetype (multiple Dockerfiles or a compose "
                "file) — a non-standard topology that needs a human decision on how "
                "each service maps to infrastructure before proceeding."
            ],
        )

    reasons: list[str] = []
    if spec.archetype == _ARCHETYPE_BACKGROUND_WORKER:
        reasons.append("Matched the background_worker archetype — less common than a web service; worth a glance.")
    if spec.monthly_budget_usd is None:
        reasons.append("No monthly budget ceiling was set — proceeding with no self-declared cost limit.")
    elif spec.monthly_budget_usd > AUTO_ADVANCE_COST_CEILING_USD:
        reasons.append(
            f"Declared budget (${spec.monthly_budget_usd:.2f}/mo) is above the "
            f"${AUTO_ADVANCE_COST_CEILING_USD:.2f}/mo auto-advance ceiling."
        )
    if spec.environment_tier.value == "production" and spec.needs_database and not spec.multi_az:
        reasons.append("Production environment with a database but Multi-AZ is not enabled.")

    if reasons:
        return DeploymentReadiness(outcome=OUTCOME_WARN, reasons=reasons)
    return DeploymentReadiness(outcome=OUTCOME_AUTO_ADVANCE, reasons=[])
