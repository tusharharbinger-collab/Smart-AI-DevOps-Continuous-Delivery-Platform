"""
services/policy-controller/tests/test_graduate_canary.py

Real gap found live: reaching 100% canary traffic never made that durable
on the cluster — the `canary` Deployment kept serving all traffic while
`baseline` sat idle running the ORIGINAL image forever, and the only
"graduation" that existed was a database label
(`projects.active_production_tag`) with nothing behind it. These tests
feed actuation_executor.graduate_canary() real inputs and assert the
actual Kubernetes API calls it makes: reading the canary's live image,
patching baseline's matching container to it (a strategic merge patch, not
the JSON-Patch RFC 6902 style the HTTPRoute needs), resetting the route to
100% baseline, and idling canary down to 0 replicas. Never touches a real
cluster — every Kubernetes client method is monkeypatched.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from src import actuation_executor


@pytest.fixture(autouse=True)
def _no_real_kubeconfig(monkeypatch):
    monkeypatch.setattr(actuation_executor, "_load_kube", lambda: None)


def _fake_canary_deployment(image="registry.internal/app:v2.0.0", container_name="app"):
    container = SimpleNamespace(image=image, name=container_name)
    return SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))))


@pytest.fixture
def captured_k8s_calls(monkeypatch):
    calls = {"read": [], "deployment_patches": [], "route_patches": [], "deployment_scales": []}

    def fake_read_namespaced_deployment(self, name, namespace, **kwargs):
        calls["read"].append({"name": name, "namespace": namespace})
        return _fake_canary_deployment()

    def fake_patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        calls["deployment_patches"].append({"name": name, "namespace": namespace, "body": body})
        return {}

    def fake_call_api(self, resource_path, method, header_params=None, body=None, **kwargs):
        calls["route_patches"].append({"resource_path": resource_path, "method": method, "body": body})
        return {}

    def fake_patch_namespaced_deployment_scale(self, name, namespace, body, **kwargs):
        calls["deployment_scales"].append({"name": name, "namespace": namespace, "body": body})
        return {}

    monkeypatch.setattr(k8s_client.AppsV1Api, "read_namespaced_deployment", fake_read_namespaced_deployment)
    monkeypatch.setattr(k8s_client.AppsV1Api, "patch_namespaced_deployment", fake_patch_namespaced_deployment)
    monkeypatch.setattr(k8s_client.ApiClient, "call_api", fake_call_api)
    monkeypatch.setattr(k8s_client.AppsV1Api, "patch_namespaced_deployment_scale", fake_patch_namespaced_deployment_scale)
    return calls


@pytest.fixture
def captured_audit_writes(monkeypatch):
    calls = []

    async def fake_record_actuation(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(actuation_executor, "record_actuation", fake_record_actuation)
    return calls


def test_graduate_copies_canarys_live_image_onto_baseline(captured_k8s_calls, captured_audit_writes):
    asyncio.run(
        actuation_executor.graduate_canary(
            pipeline_run_id="run-graduate-1",
            authorized_by="SYSTEM:auto_graduate",
            route_name="svc-route",
            namespace="production",
            canary_deployment_name="svc-canary",
            baseline_deployment_name="svc-baseline",
        )
    )

    assert captured_k8s_calls["read"] == [{"name": "svc-canary", "namespace": "production"}]
    assert len(captured_k8s_calls["deployment_patches"]) == 1
    patch = captured_k8s_calls["deployment_patches"][0]
    assert patch["name"] == "svc-baseline"
    patched_container = patch["body"]["spec"]["template"]["spec"]["containers"][0]
    assert patched_container["name"] == "app"
    assert patched_container["image"] == "registry.internal/app:v2.0.0"


def test_graduate_resets_traffic_to_100_percent_baseline(captured_k8s_calls, captured_audit_writes):
    asyncio.run(
        actuation_executor.graduate_canary(
            pipeline_run_id="run-graduate-2",
            authorized_by="SYSTEM:auto_graduate",
            route_name="svc-route",
            namespace="production",
            canary_deployment_name="svc-canary",
            baseline_deployment_name="svc-baseline",
        )
    )

    assert len(captured_k8s_calls["route_patches"]) == 1
    weight_values = {op["path"]: op["value"] for op in captured_k8s_calls["route_patches"][0]["body"]}
    assert weight_values["/spec/rules/0/backendRefs/0/weight"] == 100  # baseline
    assert weight_values["/spec/rules/0/backendRefs/1/weight"] == 0  # canary


def test_graduate_idles_canary_down_to_zero_replicas(captured_k8s_calls, captured_audit_writes):
    asyncio.run(
        actuation_executor.graduate_canary(
            pipeline_run_id="run-graduate-3",
            authorized_by="SYSTEM:auto_graduate",
            route_name="svc-route",
            namespace="production",
            canary_deployment_name="svc-canary",
            baseline_deployment_name="svc-baseline",
        )
    )

    assert len(captured_k8s_calls["deployment_scales"]) == 1
    scale = captured_k8s_calls["deployment_scales"][0]
    assert scale["name"] == "svc-canary"
    assert scale["body"]["spec"]["replicas"] == 0


def test_graduate_writes_a_graduate_audit_record(captured_k8s_calls, captured_audit_writes):
    asyncio.run(
        actuation_executor.graduate_canary(
            pipeline_run_id="run-graduate-4",
            authorized_by="SYSTEM:auto_graduate",
            route_name="svc-route",
            namespace="production",
            canary_deployment_name="svc-canary",
            baseline_deployment_name="svc-baseline",
            tenant_id="tenant-a",
        )
    )

    graduate_records = [c for c in captured_audit_writes if c["action"] == "GRADUATE"]
    assert len(graduate_records) == 1
    assert graduate_records[0]["tenant_id"] == "tenant-a"


def test_graduate_returns_the_image_it_applied(captured_k8s_calls, captured_audit_writes):
    result = asyncio.run(
        actuation_executor.graduate_canary(
            pipeline_run_id="run-graduate-5",
            authorized_by="SYSTEM:auto_graduate",
            route_name="svc-route",
            namespace="production",
            canary_deployment_name="svc-canary",
            baseline_deployment_name="svc-baseline",
        )
    )

    assert result == {"status": "GRADUATED", "new_baseline_image": "registry.internal/app:v2.0.0"}


def test_graduate_raises_when_canary_deployment_cannot_be_read(monkeypatch, captured_audit_writes):
    def failing_read(self, name, namespace, **kwargs):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(k8s_client.AppsV1Api, "read_namespaced_deployment", failing_read)

    with pytest.raises(ApiException):
        asyncio.run(
            actuation_executor.graduate_canary(
                pipeline_run_id="run-graduate-6",
                authorized_by="SYSTEM:auto_graduate",
                route_name="svc-route",
                namespace="production",
                canary_deployment_name="svc-canary",
                baseline_deployment_name="svc-baseline",
            )
        )


def test_graduate_is_still_idempotent_when_canary_scale_down_fails(captured_k8s_calls, captured_audit_writes, monkeypatch):
    """
    Mirrors emergency_rollback's own idempotency contract: the
    safety-relevant step (baseline now running the graduated image,
    traffic reset) must still succeed and record even if the best-effort
    canary scale-down fails.
    """
    def failing_scale(self, name, namespace, body, **kwargs):
        raise ApiException(status=409, reason="Conflict")

    monkeypatch.setattr(k8s_client.AppsV1Api, "patch_namespaced_deployment_scale", failing_scale)

    result = asyncio.run(
        actuation_executor.graduate_canary(
            pipeline_run_id="run-graduate-7",
            authorized_by="SYSTEM:auto_graduate",
            route_name="svc-route",
            namespace="production",
            canary_deployment_name="svc-canary",
            baseline_deployment_name="svc-baseline",
        )
    )

    assert result["status"] == "GRADUATED"
    assert len(captured_k8s_calls["deployment_patches"]) == 1
    graduate_records = [c for c in captured_audit_writes if c["action"] == "GRADUATE"]
    assert len(graduate_records) == 1
