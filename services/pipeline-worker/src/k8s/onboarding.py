"""
services/pipeline-worker/src/k8s/onboarding.py

Applies a generated service's manifests to the real Kubernetes cluster and
hands back the generated pipeline YAML for api-gateway to register.
Spec §03-multi-service-onboarding.md, deliverables 3.2/3.3.

Deployments/Services use the typed `kubernetes` client (create, falling back
to patch on 409 — same idempotency pattern as `tasks/deploy_task.py`).
HTTPRoute is created via `CustomObjectsApi.create_namespaced_custom_object`
(a plain CREATE, unlike `actuation_executor.py`'s weight-patch workaround —
that workaround exists only for PATCH's content-type bug in this client
version, which CREATE doesn't hit).
"""
import structlog
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from shared import eks_auth
from src.k8s.manifest_generator import ServiceOnboardingSpec, generate_manifests, generate_pipeline_yaml

logger = structlog.get_logger(__name__)

GROUP = "gateway.networking.k8s.io"
VERSION = "v1"
PLURAL = "httproutes"


def _load_kube():
    eks_kube_config = eks_auth.get_eks_kube_client_config()
    if eks_kube_config:
        k8s_config.load_kube_config_from_dict(eks_kube_config)
        return
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()


def _ensure_namespace_exists(core_v1: "client.CoreV1Api", namespace: str, tenant_id: str) -> None:
    """
    Phase 3 hardening: this used to assume the target namespace already
    existed — every onboarded service silently landed in whichever
    namespace the caller happened to pass (or the shared "production"
    default), meaning two different tenants' services would collide in the
    exact same namespace with nothing to stop it. Labeling with tenant_id
    makes the isolation auditable (`kubectl get ns -l tenant_id=...`), not
    just a naming convention.
    """
    body = client.V1Namespace(
        metadata=client.V1ObjectMeta(name=namespace, labels={"tenant_id": tenant_id, "managed-by": "smartcd"})
    )
    try:
        core_v1.create_namespace(body=body)
        logger.info("onboarding_namespace_created", namespace=namespace, tenant_id=tenant_id)
    except ApiException as e:
        if e.status == 409:
            logger.info("onboarding_namespace_already_exists", namespace=namespace)
        else:
            raise


def _apply_image_pull_secret(core_v1: "client.CoreV1Api", name: str, namespace: str, dockerconfigjson_b64: str) -> None:
    """
    Creates (or replaces, on retry) the real `kubernetes.io/dockerconfigjson`
    Secret a Deployment's `imagePullSecrets` references. `dockerconfigjson_b64`
    is precomputed by api-gateway's registry_router.py at credential-creation
    time — this function only ever writes it, never derives it from raw
    username/password, so the plaintext secret exists in exactly one place
    outside Kubernetes itself (Redis, until Phase 7's secrets store).
    """
    # `V1Secret.data` values are already base64 (the kubernetes client does
    # NOT re-encode them) — registry_router.py stores exactly this encoding,
    # so it is passed straight through.
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        type="kubernetes.io/dockerconfigjson",
        data={".dockerconfigjson": dockerconfigjson_b64},
    )
    try:
        core_v1.create_namespaced_secret(namespace=namespace, body=body)
        logger.info("onboarding_image_pull_secret_created", name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 409:
            core_v1.replace_namespaced_secret(name=name, namespace=namespace, body=body)
            logger.info("onboarding_image_pull_secret_replaced", name=name, namespace=namespace)
        else:
            raise


def _apply_deployment(apps_v1: "client.AppsV1Api", deployment: "client.V1Deployment", namespace: str):
    name = deployment.metadata.name
    try:
        apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment)
        logger.info("onboarding_deployment_created", name=name)
    except ApiException as e:
        if e.status == 409:
            apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=deployment)
            logger.info("onboarding_deployment_patched", name=name)
        else:
            raise


def _apply_service(core_v1: "client.CoreV1Api", service: "client.V1Service", namespace: str):
    name = service.metadata.name
    try:
        core_v1.create_namespaced_service(namespace=namespace, body=service)
        logger.info("onboarding_service_created", name=name)
    except ApiException as e:
        if e.status == 409:
            # Services are immutable on clusterIP/selector-relevant fields in
            # a full replace; a no-op patch of the ports/selector is enough
            # for the idempotent "already onboarded" case this hits on retry.
            core_v1.patch_namespaced_service(name=name, namespace=namespace, body=service)
            logger.info("onboarding_service_patched", name=name)
        else:
            raise


def _apply_http_route(custom_api: "client.CustomObjectsApi", route: dict, namespace: str):
    name = route["metadata"]["name"]
    try:
        custom_api.create_namespaced_custom_object(
            group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL, body=route
        )
        logger.info("onboarding_httproute_created", name=name)
    except ApiException as e:
        if e.status == 409:
            logger.info("onboarding_httproute_already_exists", name=name)
        else:
            raise


def onboard_service(spec: ServiceOnboardingSpec, dockerconfigjson_b64: str | None = None) -> dict:
    """
    Applies every generated manifest to the cluster (idempotent — safe to
    call twice for the same service_name) and returns the generated pipeline
    YAML text, ready for api-gateway to `POST /api/v1/pipelines` unchanged.

    `dockerconfigjson_b64` (Phase 8 follow-up, "Existing Image" wizard tab):
    when the caller resolved a registry credential for `spec.image_pull_secret_name`,
    this is that credential's precomputed Secret payload — applied BEFORE the
    Deployments that reference it via `imagePullSecrets`, so a Deployment
    never briefly exists pointing at a Secret that doesn't exist yet.
    """
    _load_kube()
    manifests = generate_manifests(spec)

    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    _ensure_namespace_exists(core_v1, spec.namespace, spec.tenant_id)
    if spec.image_pull_secret_name and dockerconfigjson_b64:
        _apply_image_pull_secret(core_v1, spec.image_pull_secret_name, spec.namespace, dockerconfigjson_b64)
    _apply_deployment(apps_v1, manifests["baseline_deployment"], spec.namespace)
    _apply_deployment(apps_v1, manifests["canary_deployment"], spec.namespace)
    _apply_service(core_v1, manifests["baseline_service"], spec.namespace)
    _apply_service(core_v1, manifests["canary_service"], spec.namespace)
    _apply_http_route(custom_api, manifests["http_route"], spec.namespace)

    pipeline_yaml = generate_pipeline_yaml(spec)
    logger.info("service_onboarded", service_name=spec.service_name, namespace=spec.namespace)
    return {
        "service_name": spec.service_name,
        "route_name": spec.route_name,
        "namespace": spec.namespace,
        "baseline_deployment": spec.baseline_deployment_name,
        "canary_deployment": spec.canary_deployment_name,
        "pipeline_yaml": pipeline_yaml,
    }
