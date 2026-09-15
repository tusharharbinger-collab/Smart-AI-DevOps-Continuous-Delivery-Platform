"""
services/pipeline-worker/tests/test_build_stage_ecr_credential.py

The build stage's dispatch in worker.py auto-generates an ECR registry
credential (via ecr_auth.get_ecr_registry_credential) whenever a project's
declared `image` is an ECR repository URI and no explicit
`registryCredentialId` was configured — so build_task.py's already-existing
generic registry-push path (_push_to_registry) fires with no manual
credential setup per project. A non-ECR image name is unaffected: it either
uses an explicitly configured credential or falls through to the existing
Kind-load path exactly as before.
"""
import base64
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
  name: ecr-build-test
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: build
      type: build
      config:
        dockerfilePath: Dockerfile
        image: "{image}"

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


def _write_manifest(tmp_path, image: str) -> str:
    path = tmp_path / "ecr-build-test.yaml"
    path.write_text(MANIFEST_YAML.format(tenant_id="tenant-1", image=image), encoding="utf-8")
    return str(path)


def test_ecr_image_auto_generates_a_registry_credential(monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path, "123456789012.dkr.ecr.us-east-1.amazonaws.com/orders-api")
    captured = {}

    def _fake_run_build_task(run_id, dockerfile, tag, repo_config=None, image_name=None, registry_credential=None):
        captured["registry_credential"] = registry_credential
        return {"status": "success", "image": f"{image_name}:{tag}"}

    fake_token = base64.b64encode(b"AWS:some-temporary-ecr-token").decode("utf-8")
    monkeypatch.setattr(worker_module, "run_build_task", _fake_run_build_task)
    monkeypatch.setattr(
        worker_module,
        "get_ecr_registry_credential",
        lambda region: {"username": "AWS", "secret": "some-temporary-ecr-token"},
    )

    orchestrator = PipelineOrchestrator(_FakeRedis())
    orchestrator.start_pipeline(manifest_path, tenant_id="tenant-1")

    assert captured["registry_credential"] == {"username": "AWS", "secret": "some-temporary-ecr-token"}


def test_non_ecr_image_does_not_auto_generate_a_credential(monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path, "registry.internal/orders-api")
    captured = {}

    def _fake_run_build_task(run_id, dockerfile, tag, repo_config=None, image_name=None, registry_credential=None):
        captured["registry_credential"] = registry_credential
        return {"status": "success", "image": f"{image_name}:{tag}"}

    def _fail_if_called(region):
        raise AssertionError("must not call ECR auth for a non-ECR image")

    monkeypatch.setattr(worker_module, "run_build_task", _fake_run_build_task)
    monkeypatch.setattr(worker_module, "get_ecr_registry_credential", _fail_if_called)

    orchestrator = PipelineOrchestrator(_FakeRedis())
    orchestrator.start_pipeline(manifest_path, tenant_id="tenant-1")

    assert captured["registry_credential"] is None
