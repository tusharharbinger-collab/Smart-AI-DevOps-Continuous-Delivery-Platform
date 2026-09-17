"""
services/pipeline-worker/tests/test_first_deployment_ecs_live_url_verification.py

Guaranteed Live Web App CI/CD (Phase 4) — real gap this closes: a genuinely
first-ever AWS ECS deployment confirmed ECS task stability and shifted the
ALB weight, but nothing ever confirmed a real visitor's request through the
real ALB actually gets a response back — exactly the "healthy target
group, 404 through the real URL" class of bug the ALB path-prefix trap
describes (CLAUDE.md). Covers: the check fires with the real computed live
URL, the result is recorded via record_live_url_verification, and — unlike
blue-green's own gated check — a failed verification is logged but never
fails the pipeline (a first deployment has no prior known-good version to
roll back to).

Same in-memory fake Redis/DB + monkeypatch pattern as
test_blue_green_rollout.py / test_first_deployment_skips_verification.py.
"""
import asyncio
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
  name: first-deploy-ecs-test
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: canary_deploy
      type: deploy
      config:
        deployment: widget-canary
        namespace: production
        containerName: widget
        image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/widget
        imageTag: v1.0.0
        deploymentTarget: aws_ecs
        awsRegion: us-east-1
        pathPrefix: /api/v1/widget

    - name: canary_verify
      type: canary_loop
      config:
        service: widget
        deploymentTarget: aws_ecs
        awsRegion: us-east-1
        pathPrefix: /api/v1/widget
        steps:
          - trafficWeight: 10
            minDuration: 120s
            minSampleSize: 100
          - trafficWeight: 100
            minDuration: 0s
            minSampleSize: 0
            requiresManualApproval: true

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


class _FakeDB:
    def __init__(self):
        self.marked_completed_for: list[str] = []
        self.live_url_verifications: list[tuple] = []

    def run_from_thread(self, coro, timeout: float = 10.0):
        return asyncio.run(coro)

    async def is_first_deployment(self, pipeline_id: str, tenant_id: str) -> bool:
        return True

    async def mark_first_deployment_completed(self, pipeline_id: str, tenant_id: str) -> None:
        self.marked_completed_for.append(pipeline_id)

    async def record_live_url_verification(self, pipeline_id: str, tenant_id: str, verified: bool) -> None:
        self.live_url_verifications.append((pipeline_id, tenant_id, verified))


@pytest.fixture
def manifest_path(tmp_path):
    path = tmp_path / "first-deploy-ecs-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def _patch_ecs_deploy_stage(monkeypatch):
    monkeypatch.setattr(
        worker_module, "deploy_ecs_canary_task",
        lambda run_id, service_name, image, image_tag, region: {"status": "deployed"},
    )
    monkeypatch.setattr(worker_module, "deploy_ecs_baseline_task", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "wait_for_ecs_service_ready", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "set_first_deployment_ecs_weights", lambda *a, **k: None)


def test_successful_first_deployment_verifies_the_real_live_url(monkeypatch, manifest_path):
    _patch_ecs_deploy_stage(monkeypatch)
    verify_calls = []
    monkeypatch.setattr(
        worker_module, "verify_live_url",
        lambda url: verify_calls.append(url) or {"verified": True, "status_code": 200, "error": None},
    )
    monkeypatch.setattr(worker_module, "AWS_ALB_BASE_URL", "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com")

    fake_db = _FakeDB()
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-1", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    assert verify_calls == ["http://smartcd-platform-alb-123.us-east-1.elb.amazonaws.com/api/v1/widget/"]
    assert fake_db.live_url_verifications == [("pipe-1", "tenant-1", True)]
    # Critically: still marked complete — this check is informational for
    # a first deployment, never a gate.
    assert fake_db.marked_completed_for == ["pipe-1"]


def test_failed_live_url_verification_is_recorded_but_never_fails_the_first_deployment(monkeypatch, manifest_path):
    # A first deployment has no prior known-good version to roll back to —
    # unlike blue-green's own gated check, this must be informational only.
    _patch_ecs_deploy_stage(monkeypatch)
    monkeypatch.setattr(
        worker_module, "verify_live_url",
        lambda url: {"verified": False, "status_code": 404, "error": "HTTP 404"},
    )
    monkeypatch.setattr(worker_module, "AWS_ALB_BASE_URL", "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com")

    fake_db = _FakeDB()
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-2", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    assert fake_db.live_url_verifications == [("pipe-1", "tenant-1", False)]
    assert fake_db.marked_completed_for == ["pipe-1"]


def test_no_alb_base_url_configured_skips_the_check_without_erroring(monkeypatch, manifest_path):
    _patch_ecs_deploy_stage(monkeypatch)
    verify_calls = []
    monkeypatch.setattr(worker_module, "verify_live_url", lambda url: verify_calls.append(url))
    monkeypatch.setattr(worker_module, "AWS_ALB_BASE_URL", None)

    fake_db = _FakeDB()
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-3", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    assert verify_calls == []
    assert fake_db.live_url_verifications == []
