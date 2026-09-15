"""
services/pipeline-worker/tests/test_first_deployment_route_freeze_window.py

Confirms `set_first_deployment_route_weights` (deploy_task.py) genuinely
consults OPA before touching the real HTTPRoute, and refuses to patch it at
all when the SAME policy (Rule 9, "FIRST_DEPLOYMENT" in
policies/delivery_guardrails.rego) says the freeze window is active — a
first deployment gets no special exemption from that one guardrail that
still applies with zero statistical evidence. `evaluate_policy_sync` is
monkeypatched (no real OPA server needed for this unit test — the Rego
rule itself is covered by policies/tests/guardrails_test.rego); the real
Kubernetes patch call is never reached when blocked, so no kube client
mocking is needed for the blocked case.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import src.tasks.deploy_task as deploy_task_module
from src.tasks.deploy_task import FreezeWindowBlockedError, set_first_deployment_route_weights


def test_first_deployment_route_patch_blocked_during_freeze_window(monkeypatch):
    def _fake_evaluate_policy_sync(payload):
        assert payload["requested_action"] == "FIRST_DEPLOYMENT"
        return {
            "allow_action": False,
            "rejection_reasons": ["Current timestamp falls within an enterprise-blocked deployment window"],
            "opa_unreachable": False,
        }

    monkeypatch.setattr(deploy_task_module, "evaluate_policy_sync", _fake_evaluate_policy_sync)

    def _fail_if_called():
        raise AssertionError("must not touch Kubernetes when OPA blocks the action")

    monkeypatch.setattr(deploy_task_module, "_load_kube", lambda: _fail_if_called())

    with pytest.raises(FreezeWindowBlockedError, match="blocked deployment window"):
        set_first_deployment_route_weights(
            "run-1", namespace="production", route_name="widget-route", pipeline_policy={"gates": {}}
        )


def test_first_deployment_route_patch_proceeds_when_not_blocked(monkeypatch):
    monkeypatch.setattr(
        deploy_task_module,
        "evaluate_policy_sync",
        lambda payload: {"allow_action": True, "rejection_reasons": [], "opa_unreachable": False},
    )
    monkeypatch.setattr(deploy_task_module, "_load_kube", lambda: None)

    patched = {}

    class _FakeApiClient:
        def call_api(self, path, method, **kwargs):
            patched["path"] = path
            patched["method"] = method
            patched["body"] = kwargs["body"]
            return {}

    monkeypatch.setattr(deploy_task_module.client, "ApiClient", lambda: _FakeApiClient())

    result = set_first_deployment_route_weights(
        "run-2", namespace="production", route_name="widget-route", pipeline_policy={"gates": {}}
    )

    assert result["status"] == "route_updated"
    assert patched["method"] == "PATCH"
    assert "widget-route" in patched["path"]
    assert patched["body"] == [
        {"op": "replace", "path": "/spec/rules/0/backendRefs/0/weight", "value": 100},
        {"op": "replace", "path": "/spec/rules/0/backendRefs/1/weight", "value": 0},
    ]
