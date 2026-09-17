"""
services/pipeline-worker/src/k8s/manifest_generator.py

Phase 3 (§03-multi-service-onboarding.md), deliverable 3.2 — turns a short
onboarding description (name, image, port, ...) into the Kubernetes objects
and the platform's own Pipeline YAML needed to run progressive delivery for
a NEW service, without anyone hand-writing YAML.

Every generated name is derived from `service_name` alone, so two onboarded
services never collide: `{service_name}-baseline`/`-canary` Deployments and
Services, `{service_name}-route` HTTPRoute. This mirrors the hand-written
`payment-service-*` objects exactly (see k8s/payments-service/*.yaml) — this
module just makes that pattern generic instead of hardcoded to one service.
"""
from dataclasses import dataclass, field

from kubernetes import client


@dataclass
class ServiceOnboardingSpec:
    service_name: str
    image: str
    baseline_tag: str
    canary_tag: str
    tenant_id: str
    port: int = 8080
    health_check_path: str = "/healthz"
    path_prefix: str | None = None
    namespace: str = "production"
    gateway_name: str = "local-edge-gateway"
    # Real gap found live (2026-09-15): the generated HTTPRoute's parentRef
    # never named a namespace, which the Gateway API spec defaults to the
    # ROUTE's own namespace — fine when `namespace` above was still
    # "production" (routes and the Gateway both there), but Phase 3's
    # tenant-namespace isolation moved every real onboarded route into its
    # own "tenant-*" namespace while the one real shared Gateway object
    # stayed fixed in "production" (see k8s/gateway/gateway-class.yaml).
    # Every project-onboarded HTTPRoute was consequently never accepted by
    # Envoy at all — confirmed live: `kubectl get httproute ... -o
    # jsonpath='{.status}'` returned empty for every one of them, and a
    # real HTTP request to a real, healthy, Ready pod returned 404 instead
    # of the app's response. Every canary rollout still correctly patched
    # `backendRefs[].weight`, but there was no accepted route for that
    # weight to ever apply to. Defaults to "production" to match where the
    # Gateway genuinely lives today; becomes configurable if that changes.
    gateway_namespace: str = "production"
    baseline_replicas: int = 1
    canary_replicas: int = 1
    cpu_request: str = "50m"
    memory_request: str = "64Mi"
    cpu_limit: str = "250m"
    memory_limit: str = "256Mi"
    metrics: list[dict] = field(default_factory=list)
    # Phase 8 follow-up ("Existing Image" wizard tab): set when the service
    # was configured with a registry credential — both Deployments then pull
    # via this Secret instead of assuming an unauthenticated/public registry.
    # None for every service onboarded before this existed, so their
    # manifests are byte-for-byte unchanged.
    image_pull_secret_name: str | None = None

    def __post_init__(self):
        if not self.path_prefix:
            self.path_prefix = f"/api/v1/{self.service_name}"

    @property
    def route_name(self) -> str:
        return f"{self.service_name}-route"

    @property
    def baseline_deployment_name(self) -> str:
        return f"{self.service_name}-baseline"

    @property
    def canary_deployment_name(self) -> str:
        return f"{self.service_name}-canary"


def _resources() -> client.V1ResourceRequirements:
    return client.V1ResourceRequirements(
        requests={"cpu": "50m", "memory": "64Mi"},
        limits={"cpu": "250m", "memory": "256Mi"},
    )


def _build_deployment(spec: ServiceOnboardingSpec, cohort: str, replicas: int) -> client.V1Deployment:
    name = spec.baseline_deployment_name if cohort == "baseline" else spec.canary_deployment_name
    tag = spec.baseline_tag if cohort == "baseline" else spec.canary_tag
    labels = {"app": spec.service_name, "deployment_cohort": cohort}
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=spec.namespace,
            labels={**labels, "delivery.devops.ai/managed-by": "autonomous-controller"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    image_pull_secrets=(
                        [client.V1LocalObjectReference(name=spec.image_pull_secret_name)]
                        if spec.image_pull_secret_name
                        else None
                    ),
                    containers=[
                        client.V1Container(
                            name=spec.service_name,
                            image=f"{spec.image}:{tag}",
                            image_pull_policy="IfNotPresent",
                            ports=[client.V1ContainerPort(container_port=spec.port)],
                            env=[
                                client.V1EnvVar(name="DEPLOYMENT_COHORT", value=cohort),
                                client.V1EnvVar(name="APP_VERSION", value=tag),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": spec.cpu_request, "memory": spec.memory_request},
                                limits={"cpu": spec.cpu_limit, "memory": spec.memory_limit},
                            ),
                            readiness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(
                                    path=spec.health_check_path, port=spec.port
                                ),
                                initial_delay_seconds=2,
                                period_seconds=5,
                            ),
                        )
                    ]
                ),
            ),
        ),
    )


def _build_service(spec: ServiceOnboardingSpec, cohort: str) -> client.V1Service:
    name = spec.baseline_deployment_name if cohort == "baseline" else spec.canary_deployment_name
    labels = {"app": spec.service_name, "deployment_cohort": cohort}
    return client.V1Service(
        metadata=client.V1ObjectMeta(name=name, namespace=spec.namespace),
        spec=client.V1ServiceSpec(
            selector=labels,
            ports=[client.V1ServicePort(port=spec.port, target_port=spec.port)],
        ),
    )


def _build_httproute(spec: ServiceOnboardingSpec) -> dict:
    """
    Returned as a plain dict (not a typed client object) because HTTPRoute is
    a Gateway API CustomResource — the generic `CustomObjectsApi` takes dicts,
    not generated model classes, the same way `actuation_executor.py` already
    treats HTTPRoute as opaque JSON it patches rather than a typed resource.
    Initial weights are 100/0 (baseline/canary), matching every rollout's
    starting point before its first verified promotion.
    """
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": spec.route_name,
            "namespace": spec.namespace,
            "labels": {"delivery.devops.ai/managed-by": "autonomous-controller"},
        },
        "spec": {
            "parentRefs": [{"name": spec.gateway_name, "namespace": spec.gateway_namespace}],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": spec.path_prefix}}],
                    "backendRefs": [
                        {"name": spec.baseline_deployment_name, "port": spec.port, "weight": 100},
                        {"name": spec.canary_deployment_name, "port": spec.port, "weight": 0},
                    ],
                }
            ],
        },
    }


def generate_manifests(spec: ServiceOnboardingSpec) -> dict:
    """Returns every Kubernetes object to apply, plus the HTTPRoute as a dict."""
    return {
        "baseline_deployment": _build_deployment(spec, "baseline", spec.baseline_replicas),
        # Started at a real replica count (not 0): unlike the hand-wired demo
        # pipeline (whose canary Deployment was pre-created outside the
        # pipeline YAML by `make deploy-sample-app`), a freshly onboarded
        # service has nothing else that would ever scale the canary up —
        # there's no separate "deploy" stage in the generated pipeline. It
        # must be live from onboarding so the very first weight shift has
        # real pods to route real traffic to.
        "canary_deployment": _build_deployment(spec, "canary", spec.canary_replicas),
        "baseline_service": _build_service(spec, "baseline"),
        "canary_service": _build_service(spec, "canary"),
        "http_route": _build_httproute(spec),
    }


def generate_pipeline_yaml(spec: ServiceOnboardingSpec) -> str:
    """
    Generates this platform's OWN declarative Pipeline YAML (§4.1) for the
    newly onboarded service — registered via the existing
    POST /api/v1/pipelines the same way the original demo pipeline was,
    so nothing downstream (trigger, rollout, verification) needs to know
    this pipeline was generated rather than hand-written.

    Honest scope note: this deliberately does NOT include a `prometheus`
    block, unlike the hand-wired demo pipeline. Phase 2 wired one static
    Prometheus scrape target per sample-app cohort
    (`monitoring/prometheus.yml`); a freshly onboarded service running as a
    brand-new Kind Deployment has no scrape target generated for it yet —
    that needs either a per-service scrape-config entry or real Kubernetes
    service discovery (a ServiceMonitor / Prometheus Operator), which is
    follow-up work, not part of this phase's scope. Omitting the block here
    is the honest choice: pipeline-worker's existing, already-tested
    synthetic-fallback path (`_synthesize_telemetry`, logged as
    `verification_using_synthetic_fallback_telemetry`) runs instead, so the
    generated pipeline still verifies and promotes/rolls back for real — on
    synthetic telemetry, not real live metrics, same as this platform's
    demo pipeline before Phase 2 wired it up.
    """
    return f"""apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: {spec.service_name}-rollout
  tenantId: "{spec.tenant_id}"
  namespace: {spec.namespace}

spec:
  stages:
    - name: progressive_verify
      type: canary_loop
      config:
        gatewayRef: {spec.gateway_name}
        service: {spec.service_name}
        routeName: {spec.route_name}
        canaryDeployment: {spec.canary_deployment_name}
        steps:
          - trafficWeight: 10
            minDuration: 120s
            minSampleSize: 100
          - trafficWeight: 25
            minDuration: 300s
            minSampleSize: 300
          - trafficWeight: 50
            minDuration: 600s
            minSampleSize: 600
          - trafficWeight: 100
            minDuration: 0s
            minSampleSize: 0
            requiresManualApproval: true

  gates:
    blockedDeployWindows: []
    manualApprovalRequired:
      beforeStages: [step_100_promotion]
      approverRoles: ["lead-sre", "platform-admin"]

  guardrails:
    autoRollbackOnVerdict: ["FAILED"]
    requireMinimumConfidence: 0.80
    minSampleSize: 100
    maxPermittedCostDeltaPercent: 15.0

  verificationConfig:
    minEvaluationWindowSeconds: 20
    metrics:
      - name: {spec.service_name.replace('-', '_')}_error_rate
        category: error_rate
        tier: critical
        alpha: 0.01
        p0: 0.005
        p1: 0.020
      - name: {spec.service_name.replace('-', '_')}_p95_latency_seconds
        category: latency
        tier: important
        alpha: 0.05
        weight: 2.5
"""
