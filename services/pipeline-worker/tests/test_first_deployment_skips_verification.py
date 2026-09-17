"""
services/pipeline-worker/tests/test_first_deployment_skips_verification.py

Real gap found live (2026-09-15): onboarding always assumed a real baseline
version already existed to compare a canary against — a project's
genuinely first-ever deployment has nothing to compare against yet, so
canary_loop's statistical verification is a category error for it, and the
baseline Deployment onboarding created (pinned to a version that was never
actually built) just sat broken forever. Matches Argo Rollouts' own
documented first-deployment behavior: ship straight to 100% on both sides,
skip analysis, and only start real canary comparisons from the SECOND
deployment onward.

No real Kubernetes/subprocess/Postgres calls here — `deploy_project_canary_task`,
`run_verification_task`, and `run_rollout_task` are all monkeypatched away
(mirrors test_deploy_stage_tag_resolution.py's approach); `_FakeDB` stands
in for the real asyncpg-backed PipelineWorkerDB.
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
  name: first-deploy-test
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
        image: registry.internal/widget
        imageTag: v1.0.0

    - name: canary_verify
      type: canary_loop
      config:
        gatewayRef: local-edge-gateway
        service: widget
        routeName: widget-route
        canaryDeployment: widget-canary
        baselineDeployment: widget-baseline
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
    """Stands in for the real asyncpg-backed PipelineWorkerDB (src/db.py)."""

    def __init__(self, first_deployment: bool):
        self._first_deployment = first_deployment
        self.marked_completed_for: list[str] = []

    def run_from_thread(self, coro, timeout: float = 10.0):
        return asyncio.run(coro)

    async def is_first_deployment(self, pipeline_id: str, tenant_id: str) -> bool:
        return self._first_deployment

    async def mark_first_deployment_completed(self, pipeline_id: str, tenant_id: str) -> None:
        self.marked_completed_for.append(pipeline_id)


@pytest.fixture
def manifest_path(tmp_path):
    path = tmp_path / "first-deploy-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def test_first_deployment_ships_straight_to_100_and_skips_verification(monkeypatch, manifest_path):
    deploy_calls = []
    verification_called = {"value": False}
    rollout_called = {"value": False}
    route_weight_calls = []

    def _fake_deploy_project_canary_task(pipeline_run_id, namespace, deployment_name, container_name, image_name, image_tag):
        deploy_calls.append(
            {
                "deployment_name": deployment_name,
                "namespace": namespace,
                "container_name": container_name,
                "image_name": image_name,
                "image_tag": image_tag,
            }
        )
        return {"status": "deployed", "deployment": deployment_name, "image": f"{image_name}:{image_tag}"}

    def _fake_run_verification_task(*args, **kwargs):
        verification_called["value"] = True
        return {"status": "HEALTHY", "confidence": 1.0, "composite_score": 100.0}

    def _fake_run_rollout_task(*args, **kwargs):
        rollout_called["value"] = True
        return {"traffic_weight": 10}

    def _fake_set_first_deployment_route_weights(pipeline_run_id, namespace, route_name, pipeline_policy):
        route_weight_calls.append({"namespace": namespace, "route_name": route_name})
        return {"status": "route_updated", "route_name": route_name, "baseline_weight": 100, "canary_weight": 0}

    liveness_checks = []

    def _fake_wait_for_deployment_ready(pipeline_run_id, namespace, deployment_name, **kwargs):
        liveness_checks.append(deployment_name)
        return {"ready": True, "deployment": deployment_name, "ready_replicas": 1, "desired_replicas": 1}

    monkeypatch.setattr(worker_module, "deploy_project_canary_task", _fake_deploy_project_canary_task)
    monkeypatch.setattr(worker_module, "run_verification_task", _fake_run_verification_task)
    monkeypatch.setattr(worker_module, "run_rollout_task", _fake_run_rollout_task)
    monkeypatch.setattr(worker_module, "set_first_deployment_route_weights", _fake_set_first_deployment_route_weights)
    monkeypatch.setattr(worker_module, "wait_for_deployment_ready", _fake_wait_for_deployment_ready)

    fake_db = _FakeDB(first_deployment=True)
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-1", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    assert verification_called["value"] is False, "first deployment must not run statistical verification"
    assert rollout_called["value"] is False, "first deployment must not run the stepped traffic ramp"

    # Two deploy calls expected: the pipeline's own "canary_deploy" stage
    # (to the canary Deployment), and the first-deployment baseline patch
    # (to the baseline Deployment) — both with the SAME image, since there
    # is no separate prior version to keep baseline on.
    assert len(deploy_calls) == 2
    canary_call = next(c for c in deploy_calls if c["deployment_name"] == "widget-canary")
    baseline_call = next(c for c in deploy_calls if c["deployment_name"] == "widget-baseline")
    assert canary_call["image_name"] == "registry.internal/widget"
    assert canary_call["image_tag"] == "v1.0.0"
    assert baseline_call["image_name"] == "registry.internal/widget"
    assert baseline_call["image_tag"] == "v1.0.0"

    assert fake_db.marked_completed_for == ["pipe-1"]
    assert route_weight_calls == [{"namespace": "production", "route_name": "widget-route"}]
    # Real gap this covers: liveness must be confirmed for BOTH sides before
    # traffic ever cuts over — the canary Deployment (already patched by the
    # pipeline's own earlier "canary_deploy" stage) and the baseline
    # Deployment (just patched above with the same image).
    assert set(liveness_checks) == {"widget-canary", "widget-baseline"}


def test_subsequent_deployment_runs_the_normal_verified_canary_path(monkeypatch, manifest_path):
    """No real bug here — confirms the existing verified path is completely
    unaffected once a project has a real prior deployment."""
    verification_called = {"value": False}
    rollout_called = {"value": False}
    baseline_patch_calls = []

    def _fake_deploy_project_canary_task(pipeline_run_id, namespace, deployment_name, container_name, image_name, image_tag):
        if deployment_name == "widget-baseline":
            baseline_patch_calls.append(deployment_name)
        return {"status": "deployed", "deployment": deployment_name, "image": f"{image_name}:{image_tag}"}

    def _fake_run_verification_task(*args, **kwargs):
        verification_called["value"] = True
        return {"status": "HEALTHY", "confidence": 0.95, "composite_score": 95.0}

    def _fake_run_rollout_task(*args, **kwargs):
        rollout_called["value"] = True
        return {"traffic_weight": 10}

    monkeypatch.setattr(worker_module, "deploy_project_canary_task", _fake_deploy_project_canary_task)
    monkeypatch.setattr(worker_module, "run_verification_task", _fake_run_verification_task)
    monkeypatch.setattr(worker_module, "run_rollout_task", _fake_run_rollout_task)

    fake_db = _FakeDB(first_deployment=False)
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-2", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    assert verification_called["value"] is True
    assert rollout_called["value"] is True
    assert baseline_patch_calls == [], "baseline must only be touched on the FIRST deployment, never afterward"
    assert fake_db.marked_completed_for == []
