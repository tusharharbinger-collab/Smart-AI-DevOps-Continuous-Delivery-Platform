"""
services/pipeline-worker/tests/test_pipeline_validate_endpoint.py

POST /pipelines/validate is the single source of truth other services (e.g.
api-gateway's AI-authoring endpoint) call rather than duplicating or
cross-importing schemas.py's safety-critical rules. This tests the
endpoint's response-shape wrapping specifically — the underlying validation
rules themselves are already thoroughly covered by test_pipeline_validation.py.
Calling the route handler directly (not via TestClient/lifespan) — no test
in this suite spins up the full app, since its lifespan opens real Redis
connections; the route handler itself is a plain async function.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.main import validate_pipeline_yaml

VALID_YAML = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: test-pipeline
  tenantId: "tenant-1"
  namespace: production

spec:
  stages:
    - name: canary_verify
      type: canary_loop
      config:
        service: test-service
        steps:
          - trafficWeight: 100
            minDuration: 60s
            minSampleSize: 100

  gates:
    blockedDeployWindows: []
    manualApprovalRequired:
      beforeStages: []
      approverRoles: []

  guardrails:
    autoRollbackOnVerdict: ["FAILED"]
    requireMinimumConfidence: 0.80
    minSampleSize: 100
    maxPermittedCostDeltaPercent: 15.0

  verificationConfig:
    minEvaluationWindowSeconds: 20
    metrics: []
"""


def test_valid_pipeline_yaml_passes():
    result = asyncio.run(validate_pipeline_yaml({"policy_yaml": VALID_YAML}))
    assert result == {"valid": True, "error": None}


def test_non_increasing_traffic_steps_rejected_with_clear_error():
    bad_yaml = VALID_YAML.replace(
        "          - trafficWeight: 100\n            minDuration: 60s\n            minSampleSize: 100\n",
        "          - trafficWeight: 50\n            minDuration: 60s\n            minSampleSize: 100\n"
        "          - trafficWeight: 30\n            minDuration: 60s\n            minSampleSize: 100\n",
    )
    result = asyncio.run(validate_pipeline_yaml({"policy_yaml": bad_yaml}))
    assert result["valid"] is False
    assert "strictly increasing" in result["error"]


def test_zero_duration_on_automated_step_rejected_with_clear_error():
    bad_yaml = VALID_YAML.replace("minDuration: 60s", "minDuration: 0s")
    result = asyncio.run(validate_pipeline_yaml({"policy_yaml": bad_yaml}))
    assert result["valid"] is False
    assert "minDuration must be > 0" in result["error"]


def test_malformed_yaml_returns_a_clear_error_not_a_500():
    result = asyncio.run(validate_pipeline_yaml({"policy_yaml": "not: valid: yaml: at: all: ]["}))
    assert result["valid"] is False
    assert result["error"] is not None
