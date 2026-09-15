"""
services/pipeline-worker/tests/test_deploy_stage_tag_resolution.py

Real bug found live (while wiring EKS/ECR support): the "deploy" stage's
dispatch in worker.py hardcoded deploy_canary_task(run_id, "v1.1.0",
deployment) regardless of what version was actually requested for this run —
independent of, and undetected by, the earlier "real per-run version
targeting" fix, which only touched the BUILD stage's own tag resolution a
few lines above. A run could therefore build one version and then deploy a
completely different (always "v1.1.0") one. Never caught before because the
seeded demo pipelines never pass a real `target_version` through this stage
in a way any prior test exercised.

Mirrors test_actuation_target_registration.py's approach: an in-memory fake
Redis, no real Kubernetes/subprocess calls (deploy_canary_task itself is
monkeypatched away — this test is only about what tag worker.py resolves
and passes to it, not deploy_task.py's own behavior, which
test_registry_preflight.py already covers).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import src.worker as worker_module
from src.worker import PipelineOrchestrator

MANIFEST_YAML = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: tag-resolution-test
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: deploy_canary
      type: deploy
      config:
        deployment: test-service-canary

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
    metrics:
      - name: error_rate
        category: error_rate
        tier: critical
        alpha: 0.01
        p0: 0.005
        p1: 0.020
"""


class _FakeRedis:
    """Minimal in-memory stand-in for the sync redis.Redis calls
    PipelineOrchestrator/ExecutionStateStore make — no real Redis needed."""

    def __init__(self):
        self.store: dict = {}

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def rpush(self, key, value):
        self.store.setdefault(key, [])
        self.store[key].append(value)

    def expire(self, key, seconds):
        pass

    def publish(self, channel, message):
        pass


@pytest.fixture
def manifest_path(tmp_path):
    path = tmp_path / "tag-resolution-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def test_deploy_stage_uses_the_runs_real_target_version_not_a_hardcoded_default(monkeypatch, manifest_path):
    captured = {}

    def _fake_deploy_canary_task(pipeline_run_id, image_tag, deployment_name="payment-service-canary"):
        captured["image_tag"] = image_tag
        captured["deployment_name"] = deployment_name
        return {"status": "deployed", "deployment": deployment_name, "image_tag": image_tag}

    monkeypatch.setattr(worker_module, "deploy_canary_task", _fake_deploy_canary_task)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    orchestrator.start_pipeline(manifest_path, tenant_id="tenant-1", target_version="v2.3.4")

    assert captured["image_tag"] == "v2.3.4"
    assert captured["deployment_name"] == "test-service-canary"


def test_deploy_stage_falls_back_to_manifest_default_when_no_target_version_given(monkeypatch, manifest_path):
    """No real bug here — confirms the fallback path (no per-run override)
    still behaves exactly as it always has."""
    captured = {}

    def _fake_deploy_canary_task(pipeline_run_id, image_tag, deployment_name="payment-service-canary"):
        captured["image_tag"] = image_tag
        return {"status": "deployed"}

    monkeypatch.setattr(worker_module, "deploy_canary_task", _fake_deploy_canary_task)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    orchestrator.start_pipeline(manifest_path, tenant_id="tenant-1")

    assert captured["image_tag"] == "v1.1.0"
