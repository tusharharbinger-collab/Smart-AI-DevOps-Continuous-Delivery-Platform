"""
services/pipeline-worker/tests/test_deprovisioning.py

Real gap found live (2026-09-15): api-gateway's DELETE /projects/{id} only
ever removed the DB row — the real Deployments/Services/HTTPRoute
onboard_service created were left running in the cluster forever. A project
recreated later with the same name hit onboard_service's own
409-then-PATCH idempotency path against these orphaned objects instead of
a clean create, silently merging old and new config (a real config-drift
bug: a stale containerPort survived alongside a freshly-declared one,
caught live while proving the live_url feature end to end). Covers
deprovision_service (onboarding.py) directly: it must delete exactly the
objects onboard_service's own naming convention would have created, ignore
404 (already gone / never created) on each independently, and never let
one missing object block deleting the rest.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kubernetes.client.rest import ApiException

from src.k8s import onboarding


class _FakeAppsV1:
    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()
        self.deleted: list[str] = []

    def delete_namespaced_deployment(self, name, namespace):
        if name in self.missing:
            raise ApiException(status=404)
        self.deleted.append(name)


class _FakeCoreV1:
    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()
        self.deleted_services: list[str] = []
        self.deleted_secrets: list[str] = []

    def delete_namespaced_service(self, name, namespace):
        if name in self.missing:
            raise ApiException(status=404)
        self.deleted_services.append(name)

    def delete_namespaced_secret(self, name, namespace):
        if name in self.missing:
            raise ApiException(status=404)
        self.deleted_secrets.append(name)


class _FakeCustomObjectsApi:
    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()
        self.deleted_routes: list[str] = []

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name):
        if name in self.missing:
            raise ApiException(status=404)
        self.deleted_routes.append(name)


def _patch_clients(monkeypatch, apps_v1, core_v1, custom_api):
    monkeypatch.setattr(onboarding, "_load_kube", lambda: None)
    monkeypatch.setattr(onboarding.client, "AppsV1Api", lambda: apps_v1)
    monkeypatch.setattr(onboarding.client, "CoreV1Api", lambda: core_v1)
    monkeypatch.setattr(onboarding.client, "CustomObjectsApi", lambda: custom_api)


def test_deletes_exactly_the_objects_onboard_service_would_have_created(monkeypatch):
    apps_v1 = _FakeAppsV1()
    core_v1 = _FakeCoreV1()
    custom_api = _FakeCustomObjectsApi()
    _patch_clients(monkeypatch, apps_v1, core_v1, custom_api)

    result = onboarding.deprovision_service("tenant-aaaaaaaa", "checkout")

    assert sorted(apps_v1.deleted) == ["checkout-baseline", "checkout-canary"]
    assert sorted(core_v1.deleted_services) == ["checkout-baseline", "checkout-canary"]
    assert custom_api.deleted_routes == ["checkout-route"]
    assert core_v1.deleted_secrets == ["checkout-registry-cred"]
    assert result["deployments"] == apps_v1.deleted
    assert result["services"] == core_v1.deleted_services
    assert result["http_routes"] == custom_api.deleted_routes
    assert result["secrets"] == core_v1.deleted_secrets


def test_ignores_404_on_individually_missing_objects(monkeypatch):
    """Real case this guards: a project whose cluster provisioning never
    fully succeeded (e.g. only the Deployments landed, not the HTTPRoute)
    must still clean up everything that DOES exist, not abort partway."""
    apps_v1 = _FakeAppsV1(missing={"checkout-canary"})
    core_v1 = _FakeCoreV1(missing={"checkout-baseline", "checkout-registry-cred"})
    custom_api = _FakeCustomObjectsApi(missing={"checkout-route"})
    _patch_clients(monkeypatch, apps_v1, core_v1, custom_api)

    result = onboarding.deprovision_service("tenant-aaaaaaaa", "checkout")

    assert result["deployments"] == ["checkout-baseline"]
    assert result["services"] == ["checkout-canary"]
    assert result["http_routes"] == []
    assert result["secrets"] == []


def test_nothing_to_deprovision_is_not_an_error(monkeypatch):
    apps_v1 = _FakeAppsV1(missing={"checkout-baseline", "checkout-canary"})
    core_v1 = _FakeCoreV1(missing={"checkout-baseline", "checkout-canary", "checkout-registry-cred"})
    custom_api = _FakeCustomObjectsApi(missing={"checkout-route"})
    _patch_clients(monkeypatch, apps_v1, core_v1, custom_api)

    result = onboarding.deprovision_service("tenant-aaaaaaaa", "checkout")

    assert result == {"deployments": [], "services": [], "http_routes": [], "secrets": []}


def test_a_real_api_error_other_than_404_still_propagates(monkeypatch):
    class _FailingAppsV1:
        def delete_namespaced_deployment(self, name, namespace):
            raise ApiException(status=500, reason="etcd unavailable")

    _patch_clients(monkeypatch, _FailingAppsV1(), _FakeCoreV1(), _FakeCustomObjectsApi())

    import pytest

    with pytest.raises(ApiException):
        onboarding.deprovision_service("tenant-aaaaaaaa", "checkout")
