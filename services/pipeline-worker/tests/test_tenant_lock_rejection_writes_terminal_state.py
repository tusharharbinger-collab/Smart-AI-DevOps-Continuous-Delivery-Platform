"""
services/pipeline-worker/tests/test_tenant_lock_rejection_writes_terminal_state.py

Real gap found live (2026-09-18): when a rollout is triggered while another
one for the same tenant+service is still running, `start_pipeline` returns
`{"status": "REJECTED", ...}` — but `main.py`'s stream consumer
(`_process_pipeline_start_message`) never checks that return value, it just
ACKs the message regardless. `pipeline_executions.status` stays 'PENDING'
forever (written once at trigger time, never updated again — a documented
trap in this codebase) and no `execution_state` row is EVER created, so the
reconciler (which only resumes rows that already have one) can never find
it either. The UI shows this identically to "about to start," permanently,
with zero indication anything went wrong. Fixed by writing a real, terminal
FAILED execution_state row at the moment of rejection — execution_state's
own CHECK constraint doesn't have a dedicated "REJECTED" value, so FAILED
with a clear reason is the correct, schema-legal terminal state.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.worker import PipelineOrchestrator

MANIFEST_YAML = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: lock-test-service
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
def manifest_path(tmp_path):
    path = tmp_path / "lock-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def test_rejected_run_is_recorded_as_a_real_terminal_failure_not_left_pending(manifest_path):
    fake_redis = _FakeRedis()
    # Pre-hold the lock exactly as a still-running concurrent pipeline would.
    fake_redis.set("lock:pipeline:tenant-1:lock-test-service", "locked", nx=True, ex=3600)

    orchestrator = PipelineOrchestrator(fake_redis, db=None)
    result = orchestrator.start_pipeline(
        manifest_path, pipeline_run_id="run-rejected", pipeline_id="pipe-1", tenant_id="tenant-1"
    )

    assert result["status"] == "REJECTED"

    import json

    saved = json.loads(fake_redis.get("state:run-rejected"))
    assert saved["status"] == "FAILED"
    assert saved["pipeline_run_id"] == "run-rejected"
    # Must actually be visible to anything polling live state — not just
    # written to the Redis key, or the UI would still show nothing.
    assert any(channel == "events:run-rejected" for channel, _ in fake_redis.published)


def test_a_free_lock_is_acquired_not_short_circuited(manifest_path, monkeypatch):
    """Regression guard: this fix must only fire on the rejection branch —
    a normal, uncontended trigger must still really acquire the lock rather
    than the rejection path firing unconditionally."""
    fake_redis = _FakeRedis()
    orchestrator = PipelineOrchestrator(fake_redis, db=None)

    calls = []
    real_acquire = orchestrator.state_store.acquire_tenant_lock

    def spying_acquire(tenant_id, service_name, ttl_seconds=3600):
        calls.append((tenant_id, service_name))
        return real_acquire(tenant_id, service_name, ttl_seconds)

    monkeypatch.setattr(orchestrator.state_store, "acquire_tenant_lock", spying_acquire)

    # The pipeline will go on to fail at a later, unmocked stage (no real
    # AWS/Docker available in this test) — irrelevant here. The only thing
    # under test is that the lock was genuinely acquired first, not that
    # the rejection branch's early return fired when the lock was free.
    try:
        orchestrator.start_pipeline(manifest_path, pipeline_run_id="run-ok", pipeline_id="pipe-1", tenant_id="tenant-1")
    except Exception:
        pass
    # The lock was genuinely acquired (real args, real call) — the
    # rejection branch's early return never fired for an uncontended
    # trigger. Whether the lock is subsequently released on failure is a
    # separate, already-correct cleanup concern, not what's under test here.
    assert calls == [("tenant-1", "lock-test-service")]
