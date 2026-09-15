"""
services/pipeline-worker/src/tasks/deploy_task.py

Applies/patches the canary Deployment against the REAL Kind cluster.
Spec §4.2.
"""
from kubernetes import client, config as k8s_config
import structlog

from shared import eks_auth
from shared.opa_client import evaluate_policy_sync
from src.preflight import preflight_check_image

logger = structlog.get_logger(__name__)

GATEWAY_GROUP = "gateway.networking.k8s.io"
GATEWAY_VERSION = "v1"
GATEWAY_PLURAL = "httproutes"


def _load_kube():
    eks_kube_config = eks_auth.get_eks_kube_client_config()
    if eks_kube_config:
        k8s_config.load_kube_config_from_dict(eks_kube_config)
        return
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()


def deploy_canary_task(pipeline_run_id: str, image_tag: str, deployment_name: str = "payment-service-canary"):
    """
    Applies or patches the canary Deployment against the Kind cluster.
    """
    image_ref = f"localhost:5001/payments:{image_tag}"
    # Phase 1 hardening: fail the stage NOW, with a clear reason, instead of
    # applying a Deployment Kubernetes will just sit forever retrying as
    # ImagePullBackOff — see preflight.py's module docstring for why this
    # checks the local Docker daemon rather than a remote registry API in
    # this stack's dev/demo setup.
    preflight_check_image(image_ref)

    try:
        _load_kube()
        apps_v1 = client.AppsV1Api()

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=deployment_name,
                namespace="production",
                labels={"app": "payments-service", "deployment_cohort": "canary"},
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(
                    match_labels={"app": "payments-service", "deployment_cohort": "canary"}
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={"app": "payments-service", "deployment_cohort": "canary"}
                    ),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="payments",
                                image=image_ref,
                                image_pull_policy="IfNotPresent",
                                ports=[client.V1ContainerPort(container_port=8080)],
                                env=[
                                    client.V1EnvVar(name="DEPLOYMENT_COHORT", value="canary"),
                                    client.V1EnvVar(name="APP_VERSION", value=image_tag),
                                ],
                            )
                        ]
                    ),
                ),
            ),
        )

        try:
            apps_v1.create_namespaced_deployment(namespace="production", body=deployment)
            logger.info("canary_deployment_created", pipeline_run_id=pipeline_run_id)
        except client.ApiException as e:
            if e.status == 409:  # already exists — patch instead
                apps_v1.patch_namespaced_deployment(
                    name=deployment_name, namespace="production", body=deployment
                )
                logger.info("canary_deployment_patched", pipeline_run_id=pipeline_run_id)
            else:
                raise

        return {"status": "deployed", "deployment": deployment_name, "image_tag": image_tag}

    except Exception as exc:
        logger.error("deploy_task_failed", error=str(exc), pipeline_run_id=pipeline_run_id)
        raise exc


def deploy_project_canary_task(
    pipeline_run_id: str,
    namespace: str,
    deployment_name: str,
    container_name: str,
    image_name: str,
    image_tag: str,
) -> dict:
    """
    Real gap found live: `generate_project_pipeline_yaml` (projects_router.py)
    never emitted a `deploy` stage at all — every wizard-onboarded project's
    pipeline went straight from `test` to `canary_verify` (traffic-shifting +
    verification only; see worker.py's `canary_loop` branch, which never
    touches a Deployment's image). The build stage would build and PUSH a
    real image to the project's real registry, but nothing ever told the
    canary Deployment onboarding created to actually run it — it just sat on
    whatever (often invalid/placeholder) image it was given at onboarding
    time, forever, regardless of how many pipeline runs "completed
    successfully."

    Unlike the legacy `deploy_canary_task` above (hardcoded to the
    payments-service demo's name/namespace/image), this patches whatever
    Deployment onboarding actually created — see manifest_generator.py's
    `_build_deployment`, which names the container after the project's own
    k8s-safe slug, not "payments". A strategic-merge-patch of just the
    container image, not a full replace, so replica count / probes / labels
    onboarding set are left untouched.
    """
    image_ref = f"{image_name}:{image_tag}"
    try:
        _load_kube()
        apps_v1 = client.AppsV1Api()
        patch = {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": image_ref}]}}}}
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch)
        logger.info(
            "project_canary_deployment_patched",
            pipeline_run_id=pipeline_run_id,
            deployment=deployment_name,
            namespace=namespace,
            image=image_ref,
        )
        return {"status": "deployed", "deployment": deployment_name, "image": image_ref}
    except Exception as exc:
        logger.error("project_deploy_task_failed", error=str(exc), pipeline_run_id=pipeline_run_id)
        raise exc


class FreezeWindowBlockedError(Exception):
    pass


def set_first_deployment_route_weights(
    pipeline_run_id: str, namespace: str, route_name: str, pipeline_policy: dict
) -> dict:
    """
    Real gap found live (2026-09-15): a project's first-ever deployment
    (worker.py's canary_loop first-deployment branch) patched the baseline/
    canary DEPLOYMENTS to the same image but never touched the real
    HTTPRoute — the platform's own recorded traffic weight said "100%" but
    nothing had told Kubernetes' actual routing object that. Deliberately
    NOT `actuation_executor.py`'s `update_traffic_weights` (policy-
    controller's own module, a separate deployable this service has no
    business importing) and NOT a reimplementation of its verdict-signature
    dance — a first deployment has no verdict to sign in the first place,
    by design. This is still real-guardrail-checked, though: freeze windows
    are the one guardrail that meaningfully applies even with zero
    statistical evidence (see policies/delivery_guardrails.rego's Rule 9,
    "FIRST_DEPLOYMENT"), checked here via the SAME OPA policy — not a
    second, Python-side copy of the day/time comparison — so there is still
    exactly one place that logic can drift.

    Sets baseline=100/canary=0: the two Deployments already carry the
    identical image at this point, so which "side" nominally holds traffic
    is cosmetic — 100/0 just matches the shape every OTHER rollout ends
    in after a real graduation, so the NEXT real canary for this project
    starts from the same clean baseline-only state as usual.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    opa_result = evaluate_policy_sync(
        {
            "requested_action": "FIRST_DEPLOYMENT",
            "pipeline_policy": pipeline_policy,
            "runtime_context": {
                "cluster_maintenance_lock": False,
                "current_day": now.strftime("%A"),
                "current_time": now.strftime("%H:%M"),
            },
        }
    )
    if not opa_result["allow_action"]:
        raise FreezeWindowBlockedError(
            f"First-deployment traffic cutover blocked by policy: {opa_result['rejection_reasons']}"
        )

    _load_kube()
    api_client = client.ApiClient()
    patch_body = [
        {"op": "replace", "path": "/spec/rules/0/backendRefs/0/weight", "value": 100},
        {"op": "replace", "path": "/spec/rules/0/backendRefs/1/weight", "value": 0},
    ]
    result = api_client.call_api(
        f"/apis/{GATEWAY_GROUP}/{GATEWAY_VERSION}/namespaces/{namespace}/{GATEWAY_PLURAL}/{route_name}",
        "PATCH",
        header_params={"Content-Type": "application/json-patch+json", "Accept": "application/json"},
        body=patch_body,
        auth_settings=["BearerToken"],
        response_type="object",
        _preload_content=True,
    )
    logger.info(
        "first_deployment_route_weights_set", pipeline_run_id=pipeline_run_id, route_name=route_name, namespace=namespace
    )
    return {"status": "route_updated", "route_name": route_name, "baseline_weight": 100, "canary_weight": 0}
