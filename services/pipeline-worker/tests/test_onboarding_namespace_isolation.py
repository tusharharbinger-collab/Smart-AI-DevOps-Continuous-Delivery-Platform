"""
services/pipeline-worker/tests/test_onboarding_namespace_isolation.py

Phase 3 hardening: every onboarded service used to land in a single shared
"production" namespace regardless of tenant — Postgres RLS already
isolates the *data*, but nothing isolated the actual Kubernetes resources
between tenants. onboard_service() now creates a (labeled, idempotent)
namespace before applying any Deployment/Service/HTTPRoute into it.
"""
import os
import sys

import pytest
from kubernetes.client.rest import ApiException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.k8s import onboarding
from src.k8s.manifest_generator import ServiceOnboardingSpec


class _FakeCoreV1:
    def __init__(self, already_exists: bool = False):
        self.already_exists = already_exists
        self.created_namespaces = []
        self.created_services = []

    def create_namespace(self, body):
        if self.already_exists:
            raise ApiException(status=409)
        self.created_namespaces.append(body)

    def create_namespaced_service(self, namespace, body):
        self.created_services.append((namespace, body))

    def patch_namespaced_service(self, name, namespace, body):
        pass


def test_ensure_namespace_exists_creates_a_labeled_namespace():
    core_v1 = _FakeCoreV1(already_exists=False)
    onboarding._ensure_namespace_exists(core_v1, "tenant-aaaaaaaa", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    assert len(core_v1.created_namespaces) == 1
    ns = core_v1.created_namespaces[0]
    assert ns.metadata.name == "tenant-aaaaaaaa"
    assert ns.metadata.labels["tenant_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_ensure_namespace_exists_is_idempotent_when_namespace_already_there():
    core_v1 = _FakeCoreV1(already_exists=True)
    onboarding._ensure_namespace_exists(core_v1, "tenant-aaaaaaaa", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")  # must not raise


def test_ensure_namespace_exists_propagates_real_api_errors():
    class _FailingCoreV1(_FakeCoreV1):
        def create_namespace(self, body):
            raise ApiException(status=500, reason="etcd unavailable")

    with pytest.raises(ApiException):
        onboarding._ensure_namespace_exists(_FailingCoreV1(), "tenant-x", "tenant-id-x")


def test_onboard_service_creates_namespace_before_any_other_resource(monkeypatch):
    """The real ordering guarantee: namespace must exist before Deployments/Services/HTTPRoute are applied into it."""
    call_order = []

    monkeypatch.setattr(onboarding, "_load_kube", lambda: None)
    monkeypatch.setattr(
        onboarding, "_ensure_namespace_exists", lambda core_v1, ns, tid: call_order.append(("namespace", ns))
    )
    monkeypatch.setattr(
        onboarding, "_apply_deployment", lambda apps_v1, dep, ns: call_order.append(("deployment", ns))
    )
    monkeypatch.setattr(onboarding, "_apply_service", lambda core_v1, svc, ns: call_order.append(("service", ns)))
    monkeypatch.setattr(onboarding, "_apply_http_route", lambda api, route, ns: call_order.append(("route", ns)))

    spec = ServiceOnboardingSpec(
        service_name="checkout",
        image="localhost:5001/checkout",
        baseline_tag="v1.0.0",
        canary_tag="v1.1.0",
        tenant_id="tenant-id-x",
        namespace="tenant-bbbbbbbb",
    )
    onboarding.onboard_service(spec)

    assert call_order[0] == ("namespace", "tenant-bbbbbbbb")
    assert all(entry[1] == "tenant-bbbbbbbb" for entry in call_order)
