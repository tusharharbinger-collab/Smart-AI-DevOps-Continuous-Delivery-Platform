"""
services/pipeline-worker/tests/test_stage_failure_rca_wiring.py

Real gap found comparing this platform against Harness's "Software
Delivery Agent": a failed pipeline stage only ever logged the raw
exception, with no explanation. worker.py's failure handler now asks
explainability-service for one and stores it at `failure_rca:{run_id}`.
Mirrors test_deploy_stage_tag_resolution.py's fake-Redis + monkeypatch
pattern — no real Redis or explainability-service needed.
"""
import json
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
  name: rca-wiring-test
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: test
      type: test
      config:
        command: "python -c \\"import sys; sys.exit(1)\\""

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

    def lrange(self, key, start, end):
        return self.store.get(key, [])

    def publish(self, channel, message):
        pass


@pytest.fixture
def manifest_path(tmp_path):
    path = tmp_path / "rca-wiring-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


class _FakeRCAResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_a_failed_stage_triggers_the_rca_call_with_the_real_error_and_logs(monkeypatch, manifest_path):
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _FakeRCAResponse({"likely_cause": "test exited non-zero", "evidence": [], "suggested_fix": "fix it"})

    monkeypatch.setattr(worker_module.requests, "post", _fake_post)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    with pytest.raises(RuntimeError):
        orchestrator.start_pipeline(manifest_path, pipeline_run_id="run-1", tenant_id="tenant-1")

    assert captured["url"] == f"{worker_module.EXPLAINABILITY_SERVICE_URL}/stage-failure-rca"
    assert captured["body"]["run_id"] == "run-1"
    assert captured["body"]["failed_stage"] == "test"
    assert "Tests failed" in captured["body"]["error_message"]

    stored = json.loads(orchestrator.redis.get("failure_rca:run-1"))
    assert stored["likely_cause"] == "test exited non-zero"


def test_explainability_service_outage_does_not_prevent_the_run_from_being_marked_failed(monkeypatch, manifest_path):
    def _raise(*args, **kwargs):
        raise ConnectionError("explainability-service unreachable")

    monkeypatch.setattr(worker_module.requests, "post", _raise)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    with pytest.raises(RuntimeError):
        orchestrator.start_pipeline(manifest_path, tenant_id="tenant-1")

    # The run must still be recorded FAILED even though the RCA call blew up.
    state_keys = [k for k in orchestrator.redis.store if k.startswith("state:")]
    assert state_keys, "expected a state:{run_id} key to have been written"
    state = json.loads(orchestrator.redis.get(state_keys[0]))
    assert state["status"] == "FAILED"
