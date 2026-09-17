"""
services/pipeline-worker/tests/test_httproute_parent_namespace.py

Real gap found live (2026-09-15): the generated HTTPRoute's parentRef never
named a namespace, which the Gateway API spec defaults to the ROUTE's own
namespace. That was harmless when `namespace` defaulted to "production" (the
route and the Gateway both lived there), but Phase 3's tenant-namespace
isolation moved every real onboarded route into its own "tenant-*"
namespace while the one real shared Gateway object stayed fixed in
"production" (k8s/gateway/gateway-class.yaml). Every project-onboarded
HTTPRoute was consequently never accepted by Envoy at all — confirmed live
by `kubectl get httproute ... -o jsonpath='{.status}'` returning empty for
every onboarded project's route, and a real HTTP request to a real, healthy,
Ready pod returning 404 instead of the app's response. Every canary rollout
this whole session still correctly patched `backendRefs[].weight`, but
there was no accepted route for that weight to ever take effect on.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.k8s.manifest_generator import ServiceOnboardingSpec, generate_manifests


def test_http_route_parent_ref_names_the_gateways_real_namespace():
    spec = ServiceOnboardingSpec(
        service_name="checkout",
        image="registry.internal/checkout",
        baseline_tag="v1.0.0",
        canary_tag="v1.1.0",
        tenant_id="tenant-id-x",
        namespace="tenant-aaaaaaaa",  # the route's own (tenant-scoped) namespace
    )
    manifests = generate_manifests(spec)
    route = manifests["http_route"]

    assert route["metadata"]["namespace"] == "tenant-aaaaaaaa"
    parent_ref = route["spec"]["parentRefs"][0]
    assert parent_ref["name"] == "local-edge-gateway"
    # The real bug: this used to be missing entirely, defaulting to the
    # route's own namespace instead of where the Gateway actually lives.
    assert parent_ref["namespace"] == "production"


def test_gateway_namespace_defaults_to_production_but_is_configurable():
    spec = ServiceOnboardingSpec(
        service_name="checkout",
        image="registry.internal/checkout",
        baseline_tag="v1.0.0",
        canary_tag="v1.1.0",
        tenant_id="tenant-id-x",
        namespace="tenant-aaaaaaaa",
        gateway_namespace="custom-gateway-ns",
    )
    manifests = generate_manifests(spec)
    assert manifests["http_route"]["spec"]["parentRefs"][0]["namespace"] == "custom-gateway-ns"
