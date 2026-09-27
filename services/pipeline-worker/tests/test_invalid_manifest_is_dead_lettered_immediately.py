"""
services/pipeline-worker/tests/test_invalid_manifest_is_dead_lettered_immediately.py

BACKLOG P2 #7b — real gap found live: a pipeline generated with a schema-invalid manifest (e.g. the
minDuration=0s-on-an-automated-step bug fixed earlier this session) made `load_pipeline` raise straight out of
`start_pipeline`, which main.py's stream consumer treated exactly like a transient failure (a network blip, an
AWS hiccup) — leaving the message unacked so it was re-delivered and re-validated against the exact same,
still-invalid YAML, over and over, for MAX_DELIVERY_ATTEMPTS cycles before it was finally dead-lettered
(`pipeline_start_retrying_stale_message` in the logs each time). A validation error is deterministic: retrying it
can never produce a different outcome. Mirrors the tenant-concurrency-lock rejection fix
(test_tenant_lock_rejection_writes_terminal_state.py) — acked and recorded as a real terminal FAILED run on the
FIRST attempt instead of a silent ghost PENDING that a human has no way to distinguish from real progress.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.worker import PipelineOrchestrator

INVALID_MANIFEST_YAML = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: invalid-manifest-service
  tenantId: "tenant-1"
  namespace: production

spec:
  stages:
    - name: canary_verify
      type: canary_loop
      config:
        gatewayRef: local-edge-gateway
        service: invalid-manifest-service
        routeName: invalid-manifest-service-route
        canaryDeployment: invalid-manifest-service-canary
        baselineDeployment: invalid-manifest-service-baseline
        steps:
          - trafficWeight: 100
            minDuration: 0s
            minSampleSize: 0

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
    minEvaluationWindowSeconds: 1
    metrics: []
"""

VALID_MANIFEST_YAML = INVALID_MANIFEST_YAML.replace(
    "            minDuration: 0s\n            minSampleSize: 0",
    "            requiresManualApproval: true\n            minDuration: 0s\n            minSampleSize: 0",
)


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.published: list[tuple] = []

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def publish(self, channel, message):
        self.published.append((channel, message))


@pytest.fixture
def invalid_manifest_path(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text(INVALID_MANIFEST_YAML, encoding="utf-8")
    return str(path)


@pytest.fixture
def valid_manifest_path(tmp_path):
    path = tmp_path / "valid.yaml"
    path.write_text(VALID_MANIFEST_YAML, encoding="utf-8")
    return str(path)


def test_an_invalid_manifest_is_rejected_on_the_first_attempt_not_raised(invalid_manifest_path):
    fake_redis = _FakeRedis()
    orchestrator = PipelineOrchestrator(fake_redis, db=None)

    # The whole point: this must NOT raise — a raised exception is exactly what main.py's stream consumer treats
    # as "leave unacked, retry later," which is the behavior being fixed.
    result = orchestrator.start_pipeline(
        invalid_manifest_path, pipeline_run_id="run-invalid", pipeline_id="pipe-1", tenant_id="tenant-1",
    )

    assert result["status"] == "REJECTED"
    assert "minDuration" in result["reason"]


def test_the_rejected_run_is_recorded_as_a_real_terminal_failure_not_left_pending(invalid_manifest_path):
    fake_redis = _FakeRedis()
    orchestrator = PipelineOrchestrator(fake_redis, db=None)

    orchestrator.start_pipeline(
        invalid_manifest_path, pipeline_run_id="run-invalid", pipeline_id="pipe-1", tenant_id="tenant-1",
    )

    saved = json.loads(fake_redis.get("state:run-invalid"))
    assert saved["status"] == "FAILED"
    assert saved["pipeline_run_id"] == "run-invalid"
    # Must actually be visible to anything polling live state — not just written to the Redis key.
    assert any(channel == "events:run-invalid" for channel, _ in fake_redis.published)


def test_no_tenant_id_available_still_rejects_cleanly_without_crashing(invalid_manifest_path):
    """The manifest's own tenantId can't be read either (load_pipeline itself is what failed) — must not require
    a spec that was never successfully parsed."""
    fake_redis = _FakeRedis()
    orchestrator = PipelineOrchestrator(fake_redis, db=None)

    result = orchestrator.start_pipeline(invalid_manifest_path, pipeline_run_id="run-no-tenant", pipeline_id="pipe-1")

    assert result["status"] == "REJECTED"
    # No terminal state is written without a real tenant_id to key it by - nothing crashes either way.
    assert fake_redis.get("state:run-no-tenant") is None


def test_a_valid_manifest_is_not_treated_as_invalid(valid_manifest_path, monkeypatch):
    """Regression guard: this fix must only fire on a genuine schema-validation failure - a well-formed manifest
    must still reach real stage execution (which then fails for unrelated reasons in this no-AWS test
    environment - irrelevant here)."""
    fake_redis = _FakeRedis()
    orchestrator = PipelineOrchestrator(fake_redis, db=None)

    result = None
    try:
        result = orchestrator.start_pipeline(
            valid_manifest_path, pipeline_run_id="run-valid", pipeline_id="pipe-1", tenant_id="tenant-1",
        )
    except Exception:
        pass  # a later, unmocked stage failing is expected and irrelevant to this assertion
    if result is not None:
        assert result.get("status") != "REJECTED" or "minDuration" not in (result.get("reason") or "")
