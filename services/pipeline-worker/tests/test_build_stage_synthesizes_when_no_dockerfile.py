"""
services/pipeline-worker/tests/test_build_stage_synthesizes_when_no_dockerfile.py

Real gap found live: the build-only preview (build_preview.py) could
synthesize a Dockerfile from a project's declared language/startCommand,
but a REAL triggered rollout's build stage only ever read `dockerfilePath`
— a project onboarded via the auto-detected-language or smartcd.yaml path
would have been proven buildable during onboarding and then fail on every
actual rollout. This confirms worker.py's build stage now synthesizes
through the exact same mechanism build_preview.py already uses and tests.
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
  name: synthesis-test
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: build
      type: build
      config:
        language: python
        startCommand: "python main.py"
        manifestPath: requirements.txt
        image: registry.internal/synthesis-test

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


@pytest.fixture
def manifest_path(tmp_path):
    path = tmp_path / "synthesis-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1"), encoding="utf-8")
    return str(path)


def test_build_stage_synthesizes_a_dockerfile_when_none_declared(monkeypatch, manifest_path):
    captured = {}

    def _fake_run_build_task(pipeline_run_id, dockerfile_path, image_tag, **kwargs):
        captured["dockerfile_path"] = dockerfile_path
        captured["dockerfile_content"] = kwargs.get("dockerfile_content")
        return {"status": "success", "image": "x", "workspace": None}

    monkeypatch.setattr(worker_module, "run_build_task", _fake_run_build_task)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    orchestrator.start_pipeline(manifest_path, tenant_id="tenant-1")

    assert captured["dockerfile_path"] == "Dockerfile"
    assert captured["dockerfile_content"] is not None
    assert "FROM python" in captured["dockerfile_content"]
    assert 'CMD ["python", "main.py"]' in captured["dockerfile_content"]


def test_build_stage_prefers_a_declared_dockerfile_path_over_synthesis(monkeypatch, tmp_path):
    manifest = MANIFEST_YAML.replace(
        "        language: python\n        startCommand: \"python main.py\"\n        manifestPath: requirements.txt\n",
        "        dockerfilePath: real/Dockerfile\n",
    ).format(tenant_id="tenant-1")
    path = tmp_path / "dockerfile-preferred.yaml"
    path.write_text(manifest, encoding="utf-8")

    captured = {}

    def _fake_run_build_task(pipeline_run_id, dockerfile_path, image_tag, **kwargs):
        captured["dockerfile_path"] = dockerfile_path
        captured["dockerfile_content"] = kwargs.get("dockerfile_content")
        return {"status": "success", "image": "x", "workspace": None}

    monkeypatch.setattr(worker_module, "run_build_task", _fake_run_build_task)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    orchestrator.start_pipeline(str(path), tenant_id="tenant-1")

    assert captured["dockerfile_path"] == "real/Dockerfile"
    assert captured["dockerfile_content"] is None
