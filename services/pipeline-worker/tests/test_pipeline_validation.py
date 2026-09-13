"""
services/pipeline-worker/tests/test_pipeline_validation.py

Phase 1 hardening: strict validation of pipeline spec traffic steps and
guardrails (src/schemas.py), wired into manifest_loader.py's load_pipeline().
Placed alongside test_dag.py (this service's existing unit-test location)
rather than a bare repo-root tests/ path, matching how every other
pipeline-worker unit test is already organized.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.schemas import (
    PipelineValidationError,
    validate_canary_steps,
    validate_guardrails,
    parse_duration_seconds,
)
from src.pipeline.manifest_loader import load_pipeline

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

_VALID_STEPS = [
    {"trafficWeight": 10, "minDuration": "120s", "minSampleSize": 100},
    {"trafficWeight": 25, "minDuration": "300s", "minSampleSize": 300},
    {"trafficWeight": 50, "minDuration": "600s", "minSampleSize": 600},
    {"trafficWeight": 100, "minDuration": "0s", "minSampleSize": 0, "requiresManualApproval": True},
]

_VALID_GUARDRAILS = {
    "autoRollbackOnVerdict": ["FAILED"],
    "requireMinimumConfidence": 0.80,
    "minSampleSize": 100,
    "maxPermittedCostDeltaPercent": 15.0,
}


def test_real_seed_pipeline_loads_cleanly():
    """The actual production pipeline YAML must never be rejected by this hardening."""
    manifest_path = os.path.join(_REPO_ROOT, "pipelines", "payments-service-policy.yaml")
    spec = load_pipeline(manifest_path)
    assert spec.name == "payments-service-rollout"


def test_valid_steps_pass():
    validate_canary_steps(_VALID_STEPS)  # must not raise


def test_valid_guardrails_pass():
    validate_guardrails(_VALID_GUARDRAILS)  # must not raise


def test_manual_approval_step_may_have_zero_sample_size_and_duration():
    """The real seed pipeline's last step (100%, human-gated) has 0/0s — must not be rejected."""
    steps = [
        {"trafficWeight": 50, "minDuration": "60s", "minSampleSize": 50},
        {
            "trafficWeight": 100,
            "minDuration": "0s",
            "minSampleSize": 0,
            "requiresManualApproval": True,
        },
    ]
    validate_canary_steps(steps)  # must not raise


@pytest.mark.parametrize(
    "steps,expected_message_fragment",
    [
        (
            [
                {"trafficWeight": 50, "minDuration": "60s", "minSampleSize": 50},
                {"trafficWeight": 30, "minDuration": "60s", "minSampleSize": 50},
            ],
            "strictly increasing",
        ),
        (
            [
                {"trafficWeight": 10, "minDuration": "60s", "minSampleSize": 50},
                {"trafficWeight": 10, "minDuration": "60s", "minSampleSize": 50},
            ],
            "strictly increasing",
        ),
        (
            [{"trafficWeight": 50, "minDuration": "60s", "minSampleSize": 50}],
            "must reach 100",
        ),
        (
            [{"trafficWeight": -5, "minDuration": "60s", "minSampleSize": 50}],
            "trafficWeight",
        ),
        (
            [{"trafficWeight": 100, "minDuration": "60s", "minSampleSize": -1}],
            "positive integer",
        ),
        (
            [{"trafficWeight": 100, "minDuration": "0s", "minSampleSize": 100}],
            "minDuration must be > 0",
        ),
        (
            [{"trafficWeight": 100, "minDuration": "not-a-duration", "minSampleSize": 100}],
            "Invalid duration",
        ),
        (
            [{"trafficWeight": 100}],  # missing minDuration/minSampleSize entirely
            "",  # any pydantic "field required" message is acceptable
        ),
        (
            [],
            "at least one traffic step",
        ),
    ],
)
def test_invalid_steps_rejected_with_clear_message(steps, expected_message_fragment):
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_canary_steps(steps)
    if expected_message_fragment:
        assert expected_message_fragment in str(exc_info.value)


@pytest.mark.parametrize(
    "guardrails,expected_message_fragment",
    [
        ({**_VALID_GUARDRAILS, "requireMinimumConfidence": 0.0}, "requireMinimumConfidence"),
        ({**_VALID_GUARDRAILS, "requireMinimumConfidence": 1.5}, "requireMinimumConfidence"),
        ({**_VALID_GUARDRAILS, "requireMinimumConfidence": -0.1}, "requireMinimumConfidence"),
        ({**_VALID_GUARDRAILS, "minSampleSize": 0}, "minSampleSize"),
        ({**_VALID_GUARDRAILS, "minSampleSize": -10}, "minSampleSize"),
        ({**_VALID_GUARDRAILS, "maxPermittedCostDeltaPercent": -1.0}, "maxPermittedCostDeltaPercent"),
    ],
)
def test_invalid_guardrails_rejected_with_clear_message(guardrails, expected_message_fragment):
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_guardrails(guardrails)
    assert expected_message_fragment in str(exc_info.value)


def test_confidence_floor_upper_bound_is_inclusive():
    """(0.0, 1.0] — exactly 1.0 must be accepted, not just values strictly below it."""
    validate_guardrails({**_VALID_GUARDRAILS, "requireMinimumConfidence": 1.0})  # must not raise


@pytest.mark.parametrize(
    "duration_str,expected_seconds",
    [("120s", 120), ("0s", 0), ("5m", 300), ("1m", 60)],
)
def test_parse_duration_seconds(duration_str, expected_seconds):
    assert parse_duration_seconds(duration_str) == expected_seconds


@pytest.mark.parametrize("bad_duration", ["120", "5h", "abc", "-10s", ""])
def test_parse_duration_seconds_rejects_malformed_input(bad_duration):
    with pytest.raises(PipelineValidationError):
        parse_duration_seconds(bad_duration)


def test_malformed_manifest_yaml_raises_clear_error(tmp_path):
    """A malformed pipeline YAML must fail at load time, not deep inside execution."""
    bad_manifest = tmp_path / "bad.yaml"
    bad_manifest.write_text(
        """
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: broken-pipeline
  tenantId: "acme-corp"
  namespace: production
spec:
  stages:
    - name: progressive_verify
      type: canary_loop
      config:
        steps:
          - trafficWeight: 50
            minDuration: 60s
            minSampleSize: 100
          - trafficWeight: 30
            minDuration: 60s
            minSampleSize: 100
  guardrails:
    requireMinimumConfidence: 0.80
    minSampleSize: 100
    maxPermittedCostDeltaPercent: 15.0
""",
        encoding="utf-8",
    )
    with pytest.raises(PipelineValidationError) as exc_info:
        load_pipeline(str(bad_manifest))
    assert "strictly increasing" in str(exc_info.value)
    assert "progressive_verify" in str(exc_info.value)
