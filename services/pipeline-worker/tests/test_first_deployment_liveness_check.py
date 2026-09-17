"""
services/pipeline-worker/tests/test_first_deployment_liveness_check.py

Real gap found live (2026-09-15): the first-deployment fast path patched
the baseline/canary Deployments and cut real HTTPRoute traffic over to them
WITHOUT ever confirming a pod actually came up healthy — "the Deployment
object was patched" and "it's actually serving traffic" were silently
treated as the same thing. Covers the fix: wait_for_deployment_ready
(deploy_task.py) is called before route weights ever change, and its
failure must propagate to the pipeline's normal FAILED path rather than
being swallowed — critically, neither the route cutover NOR
mark_first_deployment_completed may ever run for an unhealthy target, so a
retry is still correctly treated as a first deployment.

Same in-memory fake Redis/DB pattern as
test_first_deployment_skips_verification.py — no real Kubernetes calls.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import src.worker as worker_module
from src.worker import PipelineOrchestrator
from src.tasks.deploy_task import DeploymentNotReadyError

MANIFEST_YAML = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: liveness-test
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
    def __init__(self, first_deployment: bool = True):
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
    path = tmp_path / "liveness-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def test_unhealthy_deployment_fails_the_pipeline_and_never_cuts_over_traffic(monkeypatch, manifest_path):
    route_weight_calls = []

    def _fake_deploy_project_canary_task(pipeline_run_id, namespace, deployment_name, container_name, image_name, image_tag):
        return {"status": "deployed", "deployment": deployment_name, "image": f"{image_name}:{image_tag}"}

    def _fake_wait_for_deployment_ready(pipeline_run_id, namespace, deployment_name, **kwargs):
        # The canary deployment never becomes ready — a crashing container,
        # bad image, or failing readiness probe, exactly the real-world
        # cases this check exists to catch.
        raise DeploymentNotReadyError(
            f"Deployment '{deployment_name}' in namespace '{namespace}' did not become ready within 90s (ready=0/1)"
        )

    def _fake_set_first_deployment_route_weights(pipeline_run_id, namespace, route_name, pipeline_policy):
        route_weight_calls.append({"namespace": namespace, "route_name": route_name})
        return {"status": "route_updated", "route_name": route_name, "baseline_weight": 100, "canary_weight": 0}

    monkeypatch.setattr(worker_module, "deploy_project_canary_task", _fake_deploy_project_canary_task)
    monkeypatch.setattr(worker_module, "wait_for_deployment_ready", _fake_wait_for_deployment_ready)
    monkeypatch.setattr(worker_module, "set_first_deployment_route_weights", _fake_set_first_deployment_route_weights)

    fake_db = _FakeDB(first_deployment=True)
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)

    # start_pipeline re-raises on any stage exception (worker.py's outer
    # except block logs + records FAILED state + requests an RCA, then
    # `raise e`) — the caller (the Redis-stream consumer in main.py) is what
    # turns this into a FAILED run, not a returned status dict.
    with pytest.raises(DeploymentNotReadyError):
        orchestrator.start_pipeline(manifest_path, pipeline_run_id="run-1", pipeline_id="pipe-1", tenant_id="tenant-1")

    # The whole point: an unhealthy target must never receive real traffic,
    # and must never be marked as a completed first deployment — a retry
    # has to still be treated as a genuine first deployment.
    assert route_weight_calls == []
    assert fake_db.marked_completed_for == []


def test_healthy_deployment_checks_both_canary_and_baseline_before_cutover(monkeypatch, manifest_path):
    liveness_checks = []
    route_weight_calls = []

    def _fake_deploy_project_canary_task(pipeline_run_id, namespace, deployment_name, container_name, image_name, image_tag):
        return {"status": "deployed", "deployment": deployment_name, "image": f"{image_name}:{image_tag}"}

    def _fake_wait_for_deployment_ready(pipeline_run_id, namespace, deployment_name, **kwargs):
        liveness_checks.append(deployment_name)
        return {"ready": True, "deployment": deployment_name, "ready_replicas": 1, "desired_replicas": 1}

    def _fake_set_first_deployment_route_weights(pipeline_run_id, namespace, route_name, pipeline_policy):
        route_weight_calls.append({"namespace": namespace, "route_name": route_name})
        return {"status": "route_updated", "route_name": route_name, "baseline_weight": 100, "canary_weight": 0}

    monkeypatch.setattr(worker_module, "deploy_project_canary_task", _fake_deploy_project_canary_task)
    monkeypatch.setattr(worker_module, "wait_for_deployment_ready", _fake_wait_for_deployment_ready)
    monkeypatch.setattr(worker_module, "set_first_deployment_route_weights", _fake_set_first_deployment_route_weights)

    fake_db = _FakeDB(first_deployment=True)
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-2", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    # Liveness must be confirmed BEFORE the route cutover, for both sides.
    assert set(liveness_checks) == {"widget-canary", "widget-baseline"}
    assert route_weight_calls == [{"namespace": "production", "route_name": "widget-route"}]
    assert fake_db.marked_completed_for == ["pipe-1"]
