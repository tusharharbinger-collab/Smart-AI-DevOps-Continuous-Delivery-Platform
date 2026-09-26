"""
services/api-gateway/tests/test_deployment_readiness.py

Covers shared/deployment_readiness.py — the Phase 3 tiered gate
(AI_AGENTIC_ORCHESTRATION_PLAN.md §2.4) applied to the existing deploy
path. Each precedence tier gets a direct test, plus the "reasons
accumulate at the warn tier" behavior since a human should see every
concern that applies, not just the first one found.
"""
from shared.deployment_readiness import (
    AUTO_ADVANCE_COST_CEILING_USD,
    OUTCOME_AUTO_ADVANCE,
    OUTCOME_REQUIRE_APPROVAL,
    OUTCOME_WARN,
    evaluate_deployment_readiness,
)
from shared.intent_spec import EnvironmentTier, IntentSpec


def _spec(**overrides) -> IntentSpec:
    defaults = dict(environment_tier=EnvironmentTier.DEV)
    defaults.update(overrides)
    return IntentSpec(**defaults)


def test_simple_high_confidence_low_cost_project_auto_advances():
    spec = _spec(
        archetype="stateless_web_service",
        detected_confidence="high",
        monthly_budget_usd=20,
    )
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_AUTO_ADVANCE
    assert result.reasons == []


def test_low_confidence_detection_requires_approval_regardless_of_everything_else():
    spec = _spec(archetype="stateless_web_service", detected_confidence="low", monthly_budget_usd=5)
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_REQUIRE_APPROVAL
    assert "confidence" in result.reasons[0].lower()


def test_multi_service_archetype_requires_approval_even_with_high_confidence():
    spec = _spec(archetype="multi_service", detected_confidence="high", monthly_budget_usd=5)
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_REQUIRE_APPROVAL
    assert "multi_service" in result.reasons[0]


def test_low_confidence_takes_priority_over_multi_service_reason_text():
    # Precedence check: confidence is evaluated first, so a low-confidence
    # multi-service repo reports the confidence reason, not the archetype one.
    spec = _spec(archetype="multi_service", detected_confidence="low")
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_REQUIRE_APPROVAL
    assert "confidence" in result.reasons[0].lower()


def test_background_worker_archetype_warns_not_requires_approval():
    spec = _spec(archetype="background_worker", detected_confidence="high", monthly_budget_usd=10)
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_WARN
    assert any("background_worker" in r for r in result.reasons)


def test_no_budget_set_warns():
    spec = _spec(archetype="stateless_web_service", detected_confidence="high", monthly_budget_usd=None)
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_WARN
    assert any("no self-declared cost limit" in r for r in result.reasons)


def test_budget_above_ceiling_warns():
    spec = _spec(
        archetype="stateless_web_service",
        detected_confidence="high",
        monthly_budget_usd=AUTO_ADVANCE_COST_CEILING_USD + 1,
    )
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_WARN
    assert any("auto-advance ceiling" in r for r in result.reasons)


def test_budget_exactly_at_ceiling_does_not_warn_on_cost():
    spec = _spec(
        archetype="stateless_web_service",
        detected_confidence="high",
        monthly_budget_usd=AUTO_ADVANCE_COST_CEILING_USD,
    )
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_AUTO_ADVANCE


def test_production_database_without_multi_az_warns():
    spec = _spec(
        environment_tier=EnvironmentTier.PRODUCTION,
        archetype="web_service_with_database",
        detected_confidence="high",
        monthly_budget_usd=10,
        needs_database=True,
        multi_az=False,
    )
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_WARN
    assert any("Multi-AZ" in r for r in result.reasons)


def test_production_database_with_multi_az_does_not_warn_on_that_reason():
    spec = _spec(
        environment_tier=EnvironmentTier.PRODUCTION,
        archetype="web_service_with_database",
        detected_confidence="high",
        monthly_budget_usd=10,
        needs_database=True,
        multi_az=True,
    )
    result = evaluate_deployment_readiness(spec)
    assert not any("Multi-AZ" in r for r in result.reasons)


def test_multiple_warn_reasons_all_accumulate():
    spec = _spec(
        archetype="background_worker",
        detected_confidence="high",
        monthly_budget_usd=None,
    )
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_WARN
    assert len(result.reasons) == 2


def test_no_detected_confidence_at_all_does_not_force_require_approval():
    # A project onboarded via an image (no repo detection ran at all) has
    # no confidence signal to evaluate — absence of a signal must not be
    # treated as "low confidence," matching this platform's "never guess"
    # discipline for absent detection.
    spec = _spec(archetype="stateless_web_service", detected_confidence=None, monthly_budget_usd=10)
    result = evaluate_deployment_readiness(spec)
    assert result.outcome == OUTCOME_AUTO_ADVANCE
