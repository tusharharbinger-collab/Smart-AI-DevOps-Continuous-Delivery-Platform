"""
services/pipeline-worker/tests/test_wait_for_deployment_ready.py

Real gap found live (2026-09-15): nothing ever confirmed a Deployment
actually became Ready before the first-deployment fast path cut real
traffic over to it. Covers wait_for_deployment_ready (deploy_task.py)
directly: it must poll the real Kubernetes API (via the same client this
module already uses everywhere else, never a `kubectl` subprocess — this
container has no such binary), succeed once ready_replicas meets the
desired count, and raise DeploymentNotReadyError on timeout rather than
hanging or silently returning success.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.tasks.deploy_task as deploy_task_module
from src.tasks.deploy_task import DeploymentNotReadyError, wait_for_deployment_ready

import pytest


def _fake_deployment(desired: int, ready: int):
    return SimpleNamespace(spec=SimpleNamespace(replicas=desired), status=SimpleNamespace(ready_replicas=ready))


def test_returns_ready_immediately_when_deployment_is_already_healthy(monkeypatch):
    monkeypatch.setattr(deploy_task_module, "_load_kube", lambda: None)

    class _FakeAppsV1:
        def read_namespaced_deployment(self, name, namespace):
            return _fake_deployment(desired=1, ready=1)

    monkeypatch.setattr(deploy_task_module.client, "AppsV1Api", lambda: _FakeAppsV1())

    result = wait_for_deployment_ready(
        "run-1", namespace="production", deployment_name="widget-canary", timeout_seconds=5, poll_interval_seconds=0.01
    )
    assert result["ready"] is True
    assert result["ready_replicas"] == 1


def test_polls_until_ready_replicas_catches_up(monkeypatch):
    monkeypatch.setattr(deploy_task_module, "_load_kube", lambda: None)
    call_count = {"value": 0}

    class _FakeAppsV1:
        def read_namespaced_deployment(self, name, namespace):
            call_count["value"] += 1
            # Not ready for the first two polls, ready on the third.
            ready = 1 if call_count["value"] >= 3 else 0
            return _fake_deployment(desired=1, ready=ready)

    monkeypatch.setattr(deploy_task_module.client, "AppsV1Api", lambda: _FakeAppsV1())

    result = wait_for_deployment_ready(
        "run-2", namespace="production", deployment_name="widget-canary", timeout_seconds=5, poll_interval_seconds=0.01
    )
    assert result["ready"] is True
    assert call_count["value"] == 3


def test_raises_a_clear_error_on_timeout_instead_of_hanging_or_silently_succeeding(monkeypatch):
    monkeypatch.setattr(deploy_task_module, "_load_kube", lambda: None)

    class _FakeAppsV1:
        def read_namespaced_deployment(self, name, namespace):
            # Never becomes ready — the real-world case of a crashing
            # container or a failing readiness probe.
            return _fake_deployment(desired=1, ready=0)

    monkeypatch.setattr(deploy_task_module.client, "AppsV1Api", lambda: _FakeAppsV1())

    with pytest.raises(DeploymentNotReadyError, match="did not become ready"):
        wait_for_deployment_ready(
            "run-3", namespace="production", deployment_name="widget-canary", timeout_seconds=0.05, poll_interval_seconds=0.01
        )
