"""
services/pipeline-worker/tests/test_blue_green_rollout.py

Guaranteed Live Web App CI/CD (P0) — real gap this closes: a genuinely new
web app with zero real visitors can never accumulate the samples
statistical canary verification needs, so a project onboarded with
deploy_mode: blue_green would sit DEGRADED ("insufficient samples")
forever and never reach its live URL, even though the build and deploy
themselves succeeded. Covers worker.py's is_blue_green branch: the healthy
path (health checks pass, cutover happens, live URL verifies, graduation
happens, statistical verification is never invoked), the unhealthy-target-
group path (cutover never called), and the post-cutover-verification-fails
path (rollback fires, pipeline fails, graduation never called).

Same in-memory fake Redis/DB + monkeypatch pattern as
test_first_deployment_skips_verification.py — no real AWS/Kubernetes calls.
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
  name: blue-green-test
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
        imageTag: v1.2.0
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
        deploymentStrategy: blue_green
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
        self.live_url_verifications: list[tuple] = []
        self.actuations: list[dict] = []
        self.cost_analyses: list[dict] = []

    def run_from_thread(self, coro, timeout: float = 10.0):
        return asyncio.run(coro)

    async def is_first_deployment(self, pipeline_id: str, tenant_id: str) -> bool:
        # Must never even be consulted — is_blue_green is checked BEFORE
        # is_first_deploy and `continue`s past it entirely.
        raise AssertionError("is_first_deployment should never be called for a blue-green rollout")

    async def record_live_url_verification(self, pipeline_id: str, tenant_id: str, verified: bool) -> None:
        self.live_url_verifications.append((pipeline_id, tenant_id, verified))

    async def record_actuation(self, tenant_id, pipeline_run_id, action, canary_weight, baseline_weight, authorized_by, **kwargs):
        self.actuations.append(
            {
                "tenant_id": tenant_id, "pipeline_run_id": pipeline_run_id, "action": action,
                "canary_weight": canary_weight, "baseline_weight": baseline_weight, "authorized_by": authorized_by,
            }
        )

    async def record_cost_analysis(self, tenant_id, pipeline_run_id, baseline_cost, canary_cost, delta_percent, **kwargs):
        self.cost_analyses.append(
            {
                "tenant_id": tenant_id, "pipeline_run_id": pipeline_run_id,
                "baseline_cost": baseline_cost, "canary_cost": canary_cost, "delta_percent": delta_percent,
            }
        )


@pytest.fixture
def manifest_path(tmp_path):
    path = tmp_path / "blue-green-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def _patch_deploy_stage(monkeypatch):
    monkeypatch.setattr(
        worker_module, "deploy_ecs_canary_task",
        lambda run_id, service_name, image, image_tag, region: {"status": "deployed"},
    )


def test_healthy_blue_green_rollout_cuts_over_verifies_and_graduates_without_any_statistical_verification(
    monkeypatch, manifest_path
):
    _patch_deploy_stage(monkeypatch)
    calls = {"wait_ready": [], "wait_healthy": [], "cutover": [], "verify": [], "graduate": [], "rollback": [], "cost": []}

    monkeypatch.setattr(
        worker_module, "wait_for_ecs_service_ready",
        lambda region, service_name, cohort: calls["wait_ready"].append((service_name, cohort)),
    )
    monkeypatch.setattr(
        worker_module, "wait_for_target_group_healthy",
        lambda region, tg_name: calls["wait_healthy"].append(tg_name) or {"healthy": True},
    )
    monkeypatch.setattr(
        worker_module, "cutover_blue_green_ecs_weights",
        lambda run_id, service_name, path_prefix, region, pipeline_policy: calls["cutover"].append(service_name),
    )
    monkeypatch.setattr(
        worker_module, "verify_live_url",
        lambda url: calls["verify"].append(url) or {"verified": True, "status_code": 200, "error": None},
    )
    monkeypatch.setattr(
        worker_module, "graduate_blue_green_ecs",
        lambda run_id, service_name, image, image_tag, path_prefix, region: calls["graduate"].append(
            (service_name, image, image_tag)
        ),
    )
    monkeypatch.setattr(
        worker_module, "rollback_blue_green_ecs_weights",
        lambda *a, **k: calls["rollback"].append(1),
    )
    monkeypatch.setattr(
        worker_module, "compute_blue_green_cost",
        lambda service_name, region, max_permitted_delta_percent: calls["cost"].append(service_name)
        or {"baseline_cost_usd": 0.081, "canary_cost_usd": 0.081, "delta_percent": 0.0, "delta_usd": 0.0, "exceeds_policy_limit": False},
    )

    def fail_run_verification_task(*args, **kwargs):
        raise AssertionError("statistical verification must never run for a blue-green rollout")

    def fail_run_rollout_task(*args, **kwargs):
        raise AssertionError("the stepped traffic ramp must never run for a blue-green rollout")

    monkeypatch.setattr(worker_module, "run_verification_task", fail_run_verification_task)
    monkeypatch.setattr(worker_module, "run_rollout_task", fail_run_rollout_task)
    monkeypatch.setattr(worker_module, "AWS_ALB_BASE_URL", "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com")

    fake_db = _FakeDB()
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-1", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "COMPLETED"
    assert calls["wait_ready"] == [("widget", "canary")]
    assert calls["wait_healthy"] == ["widget-canary"]
    assert calls["cutover"] == ["widget"]
    assert calls["verify"] == ["http://smartcd-platform-alb-123.us-east-1.elb.amazonaws.com/api/v1/widget/"]
    assert calls["graduate"] == [("widget", "123456789012.dkr.ecr.us-east-1.amazonaws.com/widget", "v1.2.0")]
    assert calls["rollback"] == []
    assert fake_db.live_url_verifications == [("pipe-1", "tenant-1", True)]
    # Real gap found live (2026-09-17): this path never wrote a single
    # audit_ledger row — the Audit Ledger UI correctly showed "0 total
    # actions" for a run that had genuinely cut over real production
    # traffic. Both the cutover and the graduation must be recorded.
    actions = [a["action"] for a in fake_db.actuations]
    assert actions == ["BLUE_GREEN_CUTOVER", "GRADUATE"]
    cutover_entry = fake_db.actuations[0]
    assert cutover_entry["pipeline_run_id"] == "run-1"
    assert cutover_entry["tenant_id"] == "tenant-1"
    assert cutover_entry["canary_weight"] == 100 and cutover_entry["baseline_weight"] == 0
    graduate_entry = fake_db.actuations[1]
    assert graduate_entry["canary_weight"] == 0 and graduate_entry["baseline_weight"] == 100
    # Real gap found live (2026-09-17): cost_analysis had 0 rows for any
    # blue-green rollout ever — RULE 7's cost guardrail and the Reports &
    # Cost UI both read from a table only the verdict-driven path wrote to.
    assert calls["cost"] == ["widget"]
    assert fake_db.cost_analyses == [
        {"tenant_id": "tenant-1", "pipeline_run_id": "run-1", "baseline_cost": 0.081, "canary_cost": 0.081, "delta_percent": 0.0}
    ]


def test_unhealthy_target_group_fails_the_pipeline_and_never_cuts_over(monkeypatch, manifest_path):
    _patch_deploy_stage(monkeypatch)
    cutover_calls = []

    monkeypatch.setattr(worker_module, "wait_for_ecs_service_ready", lambda *a, **k: None)

    def fake_wait_healthy(region, tg_name):
        raise RuntimeError(f"Target group '{tg_name}' did not report all targets healthy within 120s")

    monkeypatch.setattr(worker_module, "wait_for_target_group_healthy", fake_wait_healthy)
    monkeypatch.setattr(
        worker_module, "cutover_blue_green_ecs_weights",
        lambda *a, **k: cutover_calls.append(1),
    )

    fake_db = _FakeDB()
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    with pytest.raises(RuntimeError, match="did not report all targets healthy"):
        orchestrator.start_pipeline(manifest_path, pipeline_run_id="run-2", pipeline_id="pipe-1", tenant_id="tenant-1")

    assert cutover_calls == []
    assert fake_db.live_url_verifications == []


def test_failed_post_cutover_verification_rolls_back_and_never_graduates(monkeypatch, manifest_path):
    _patch_deploy_stage(monkeypatch)
    calls = {"cutover": [], "rollback": [], "graduate": []}

    monkeypatch.setattr(worker_module, "wait_for_ecs_service_ready", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "wait_for_target_group_healthy", lambda *a, **k: {"healthy": True})
    monkeypatch.setattr(
        worker_module, "cutover_blue_green_ecs_weights",
        lambda run_id, service_name, path_prefix, region, pipeline_policy: calls["cutover"].append(service_name),
    )
    # Simulates AWS being unreachable for the cost snapshot specifically —
    # must never affect the rollout's own real outcome (fail-soft contract).
    monkeypatch.setattr(worker_module, "compute_blue_green_cost", lambda *a, **k: None)
    monkeypatch.setattr(
        worker_module, "verify_live_url",
        lambda url: {"verified": False, "status_code": None, "error": "Connection refused"},
    )
    monkeypatch.setattr(
        worker_module, "rollback_blue_green_ecs_weights",
        lambda run_id, service_name, path_prefix, region: calls["rollback"].append(service_name),
    )
    monkeypatch.setattr(
        worker_module, "graduate_blue_green_ecs",
        lambda *a, **k: calls["graduate"].append(1),
    )
    monkeypatch.setattr(worker_module, "AWS_ALB_BASE_URL", "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com")

    fake_db = _FakeDB()
    orchestrator = PipelineOrchestrator(_FakeRedis(), db=fake_db)
    with pytest.raises(RuntimeError, match="failed live-URL verification"):
        orchestrator.start_pipeline(manifest_path, pipeline_run_id="run-3", pipeline_id="pipe-1", tenant_id="tenant-1")

    assert calls["cutover"] == ["widget"]
    assert calls["rollback"] == ["widget"]
    assert calls["graduate"] == []
    # The failure is still recorded — a failed verification is a real,
    # visible signal, not silently swallowed.
    assert fake_db.live_url_verifications == [("pipe-1", "tenant-1", False)]
    # Both the (later-reverted) cutover AND the rollback must be audited —
    # a rolled-back action is still a real actuation that happened, not
    # something to omit from the ledger.
    actions = [a["action"] for a in fake_db.actuations]
    assert actions == ["BLUE_GREEN_CUTOVER", "ROLLBACK"]
    assert fake_db.actuations[1]["canary_weight"] == 0 and fake_db.actuations[1]["baseline_weight"] == 100
