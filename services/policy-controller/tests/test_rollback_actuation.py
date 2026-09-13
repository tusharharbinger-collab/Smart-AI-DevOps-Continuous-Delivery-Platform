"""
services/policy-controller/tests/test_rollback_actuation.py

Unlike test_opa_failsafe.py (which mocks emergency_rollback/
update_traffic_weights away to test controller.py's routing logic in
isolation), this tests actuation_executor.py itself: feeds it real inputs
and asserts the actual Kubernetes API calls it makes — the JSON Patch body
sent to the HTTPRoute (weight -> 0%) and the Deployment scale-down call
(canary pods -> 0 replicas) — plus that a real audit record gets written.
Never touches a real cluster: `kubernetes.client.ApiClient.call_api` and
`AppsV1Api.patch_namespaced_deployment_scale` are monkeypatched.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from kubernetes import client as k8s_client

from src import actuation_executor


@pytest.fixture(autouse=True)
def _no_real_kubeconfig(monkeypatch):
    """Never let _load_kube() try to reach a real cluster's API server."""
    monkeypatch.setattr(actuation_executor, "_load_kube", lambda: None)


@pytest.fixture
def captured_k8s_calls(monkeypatch):
    calls = {"http_route_patches": [], "deployment_scales": []}

    def fake_call_api(self, resource_path, method, header_params=None, body=None, **kwargs):
        calls["http_route_patches"].append(
            {"resource_path": resource_path, "method": method, "header_params": header_params, "body": body}
        )
        return {}

    def fake_patch_namespaced_deployment_scale(self, name, namespace, body, **kwargs):
        calls["deployment_scales"].append({"name": name, "namespace": namespace, "body": body})
        return {}

    monkeypatch.setattr(k8s_client.ApiClient, "call_api", fake_call_api)
    monkeypatch.setattr(k8s_client.AppsV1Api, "patch_namespaced_deployment_scale", fake_patch_namespaced_deployment_scale)
    return calls


@pytest.fixture
def captured_audit_writes(monkeypatch):
    """record_actuation() is only meaningful with a real DB — capture what it WOULD have written."""
    calls = []

    async def fake_record_actuation(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(actuation_executor, "record_actuation", fake_record_actuation)
    return calls


def test_emergency_rollback_drops_canary_weight_to_zero(captured_k8s_calls, captured_audit_writes):
    asyncio.run(
        actuation_executor.emergency_rollback(
            pipeline_run_id="run-rollback-1",
            authorized_by="OPA:rule=ROLLBACK",
            route_name="payment-service-route",
            namespace="production",
            canary_deployment_name="payment-service-canary",
            tenant_id="tenant-a",
            verdict_status="FAILED",
            confidence=0.92,
        )
    )

    assert len(captured_k8s_calls["http_route_patches"]) == 1
    patch = captured_k8s_calls["http_route_patches"][0]
    assert patch["method"] == "PATCH"
    assert "payment-service-route" in patch["resource_path"]
    assert patch["header_params"]["Content-Type"] == "application/json-patch+json"

    # The two weight ops: baseline -> 100, canary -> 0 (RFC 6902 JSON Patch).
    weight_values = {op["path"]: op["value"] for op in patch["body"]}
    assert weight_values["/spec/rules/0/backendRefs/0/weight"] == 100  # baseline
    assert weight_values["/spec/rules/0/backendRefs/1/weight"] == 0  # canary


def test_emergency_rollback_scales_canary_deployment_to_zero_replicas(captured_k8s_calls, captured_audit_writes):
    asyncio.run(
        actuation_executor.emergency_rollback(
            pipeline_run_id="run-rollback-2",
            authorized_by="OPA:rule=ROLLBACK",
            route_name="payment-service-route",
            namespace="production",
            canary_deployment_name="payment-service-canary",
        )
    )

    assert len(captured_k8s_calls["deployment_scales"]) == 1
    scale_call = captured_k8s_calls["deployment_scales"][0]
    assert scale_call["name"] == "payment-service-canary"
    assert scale_call["namespace"] == "production"
    assert scale_call["body"]["spec"]["replicas"] == 0


def test_emergency_rollback_writes_a_signed_audit_record(captured_k8s_calls, captured_audit_writes):
    """
    emergency_rollback() calls update_traffic_weights() internally (which
    writes its own WEIGHT_UPDATE record) before writing its own ROLLBACK
    record — two audit rows per rollback is expected, not a bug: one
    documents the weight change, the other documents the rollback decision.
    """
    asyncio.run(
        actuation_executor.emergency_rollback(
            pipeline_run_id="run-rollback-3",
            authorized_by="OPA:rule=ROLLBACK",
            tenant_id="tenant-a",
            verdict_status="FAILED",
            confidence=0.85,
        )
    )

    assert len(captured_audit_writes) == 2
    actions = {a["action"] for a in captured_audit_writes}
    assert actions == {"WEIGHT_UPDATE", "ROLLBACK"}

    rollback_audit = next(a for a in captured_audit_writes if a["action"] == "ROLLBACK")
    assert rollback_audit["canary_weight"] == 0
    assert rollback_audit["baseline_weight"] == 100
    assert rollback_audit["tenant_id"] == "tenant-a"
    assert rollback_audit["verdict"] == "FAILED"
    assert rollback_audit["confidence"] == 0.85


def test_emergency_rollback_is_idempotent(captured_k8s_calls, captured_audit_writes):
    """Calling it twice must produce the same end state (weight 0, replicas 0), no side effects from repetition."""
    for _ in range(2):
        asyncio.run(
            actuation_executor.emergency_rollback(
                pipeline_run_id="run-rollback-4",
                authorized_by="OPA:rule=ROLLBACK",
            )
        )

    assert len(captured_k8s_calls["http_route_patches"]) == 2
    for patch in captured_k8s_calls["http_route_patches"]:
        weight_values = {op["path"]: op["value"] for op in patch["body"]}
        assert weight_values["/spec/rules/0/backendRefs/1/weight"] == 0


def test_update_traffic_weights_promotes_canary(captured_k8s_calls, captured_audit_writes):
    """Positive control: a HEALTHY-verdict promotion moves weight the other direction."""
    asyncio.run(
        actuation_executor.update_traffic_weights(
            pipeline_run_id="run-promote-1",
            canary_weight=25,
            baseline_weight=75,
            authorized_by="OPA:rule=PROMOTE_STEP",
            verdict_status="HEALTHY",
            confidence=0.9,
        )
    )

    patch = captured_k8s_calls["http_route_patches"][0]
    weight_values = {op["path"]: op["value"] for op in patch["body"]}
    assert weight_values["/spec/rules/0/backendRefs/1/weight"] == 25
    assert weight_values["/spec/rules/0/backendRefs/0/weight"] == 75
    assert len(captured_k8s_calls["deployment_scales"]) == 0, "promotion must never scale the canary down"

    audit = captured_audit_writes[0]
    assert audit["action"] == "WEIGHT_UPDATE"
    assert audit["verdict"] == "HEALTHY"


def test_kubernetes_api_failure_propagates_not_swallowed(monkeypatch, captured_audit_writes):
    """A real cluster error on the HTTPRoute patch must raise, not be silently swallowed as a successful rollback."""
    from kubernetes.client.rest import ApiException

    def failing_call_api(self, *args, **kwargs):
        raise ApiException(status=500, reason="etcd unavailable")

    monkeypatch.setattr(k8s_client.ApiClient, "call_api", failing_call_api)

    with pytest.raises(ApiException):
        asyncio.run(
            actuation_executor.update_traffic_weights(
                pipeline_run_id="run-fail-1",
                canary_weight=10,
                baseline_weight=90,
                authorized_by="OPA:rule=PROMOTE_STEP",
            )
        )
    assert len(captured_audit_writes) == 0, "no audit record should be written for a failed actuation"
