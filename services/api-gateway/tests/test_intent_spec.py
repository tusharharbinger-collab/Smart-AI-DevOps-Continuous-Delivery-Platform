"""
services/api-gateway/tests/test_intent_spec.py

Covers shared/intent_spec.py — the structured object the Requirements Form
(AI_AGENTIC_ORCHESTRATION_PLAN.md §2.1/§2.5) locks in. Two things matter
most here: (1) validation catches an inconsistent submission (multi_az with
no database, max < min instances) before it ever reaches a generation step,
and (2) apply_tier_defaults only ever fills a field the human left unset —
it must never override a real value they provided.
"""
import pytest
from pydantic import ValidationError

from shared.intent_spec import (
    FARGATE_TIER_DEFAULTS,
    DatabaseType,
    EnvironmentTier,
    IntentSpec,
    apply_tier_defaults,
)


def test_minimal_valid_spec_only_needs_a_tier():
    spec = IntentSpec(environment_tier=EnvironmentTier.DEV)
    assert spec.environment_tier == EnvironmentTier.DEV
    assert spec.needs_database is False


def test_multi_az_without_database_is_rejected():
    with pytest.raises(ValidationError, match="multi_az only applies to a database"):
        IntentSpec(environment_tier=EnvironmentTier.PRODUCTION, multi_az=True, needs_database=False)


def test_multi_az_with_database_is_valid():
    spec = IntentSpec(environment_tier=EnvironmentTier.PRODUCTION, multi_az=True, needs_database=True)
    assert spec.multi_az is True


def test_database_type_without_needs_database_is_rejected():
    with pytest.raises(ValidationError, match="database_type set but needs_database is False"):
        IntentSpec(environment_tier=EnvironmentTier.DEV, database_type=DatabaseType.POSTGRES, needs_database=False)


def test_max_instances_below_min_is_rejected():
    with pytest.raises(ValidationError, match="max_instances must be >= min_instances"):
        IntentSpec(environment_tier=EnvironmentTier.DEV, min_instances=4, max_instances=2)


def test_max_instances_equal_to_min_is_valid():
    spec = IntentSpec(environment_tier=EnvironmentTier.DEV, min_instances=2, max_instances=2)
    assert spec.max_instances == 2


def test_negative_budget_is_rejected():
    with pytest.raises(ValidationError):
        IntentSpec(environment_tier=EnvironmentTier.DEV, monthly_budget_usd=-10)


def test_negative_expected_rps_is_rejected():
    with pytest.raises(ValidationError):
        IntentSpec(environment_tier=EnvironmentTier.DEV, expected_rps=-1)


# ─────────────── apply_tier_defaults ───────────────


def test_dev_tier_defaults_are_filled_when_unset():
    spec = IntentSpec(environment_tier=EnvironmentTier.DEV)
    normalized = apply_tier_defaults(spec)
    expected = FARGATE_TIER_DEFAULTS[EnvironmentTier.DEV]
    assert normalized.fargate_cpu == expected["cpu"]
    assert normalized.fargate_memory_mib == expected["memory_mib"]
    assert normalized.min_instances == expected["min_instances"]
    assert normalized.max_instances == expected["max_instances"]


def test_production_tier_defaults_to_multi_az_only_when_database_needed():
    spec = IntentSpec(environment_tier=EnvironmentTier.PRODUCTION, needs_database=True)
    normalized = apply_tier_defaults(spec)
    assert normalized.multi_az is True
    assert normalized.min_instances == FARGATE_TIER_DEFAULTS[EnvironmentTier.PRODUCTION]["min_instances"]


def test_production_tier_does_not_force_multi_az_without_a_database():
    spec = IntentSpec(environment_tier=EnvironmentTier.PRODUCTION, needs_database=False)
    normalized = apply_tier_defaults(spec)
    assert normalized.multi_az is False


def test_apply_tier_defaults_never_overrides_a_value_the_human_set():
    # Real gap this guards against: a human explicitly choosing a bigger
    # Fargate size than their tier's default must not get silently
    # downsized back to the default.
    spec = IntentSpec(environment_tier=EnvironmentTier.DEV, fargate_cpu=2048, fargate_memory_mib=4096, min_instances=3)
    normalized = apply_tier_defaults(spec)
    assert normalized.fargate_cpu == 2048
    assert normalized.fargate_memory_mib == 4096
    assert normalized.min_instances == 3
    # max_instances was left unset. The tier's own default (1) is BELOW the
    # min_instances the human set (3), so it must be bumped up to match
    # rather than producing an invalid min>max spec out of a value they
    # never touched.
    assert normalized.max_instances == 3


def test_staging_tier_defaults():
    spec = IntentSpec(environment_tier=EnvironmentTier.STAGING)
    normalized = apply_tier_defaults(spec)
    expected = FARGATE_TIER_DEFAULTS[EnvironmentTier.STAGING]
    assert normalized.fargate_cpu == expected["cpu"]
    assert normalized.max_instances == expected["max_instances"]


def test_archetype_and_detection_hints_are_carried_through_unchanged():
    spec = IntentSpec(
        environment_tier=EnvironmentTier.DEV,
        archetype="web_service_with_database",
        detected_database_hint="psycopg2-binary",
    )
    normalized = apply_tier_defaults(spec)
    assert normalized.archetype == "web_service_with_database"
    assert normalized.detected_database_hint == "psycopg2-binary"
