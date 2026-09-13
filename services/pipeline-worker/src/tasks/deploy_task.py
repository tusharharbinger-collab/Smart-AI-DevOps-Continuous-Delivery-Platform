"""
services/pipeline-worker/src/tasks/deploy_task.py

Applies/patches the canary Deployment against the REAL Kind cluster.
Spec §4.2.
"""
from kubernetes import client, config as k8s_config
import structlog

from src.preflight import preflight_check_image

logger = structlog.get_logger(__name__)


def _load_kube():
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
