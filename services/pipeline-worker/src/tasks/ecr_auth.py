"""
services/pipeline-worker/src/tasks/ecr_auth.py

Generates a temporary ECR registry credential in the exact
{"username": ..., "secret": ...} shape build_task.py::_push_to_registry
already expects from registry_router.py-stored credentials — ECR just never
had a credential source wired in before. ECR authorization tokens expire
after 12 hours; a fresh one is generated per build rather than cached, since
build stages are infrequent, short-lived operations.
"""
import base64

import boto3
import structlog

logger = structlog.get_logger(__name__)

ECR_URI_MARKER = ".dkr.ecr."


def is_ecr_image(image_name: str) -> bool:
    """True if `image_name` looks like an ECR repository URI, e.g.
    123456789012.dkr.ecr.us-east-1.amazonaws.com/payments."""
    return ECR_URI_MARKER in image_name


def get_ecr_registry_credential(region: str) -> dict:
    """
    Calls ECR's GetAuthorizationToken and returns
    {"username": "AWS", "secret": <token>} — ready to pass straight into
    build_task.py::_push_to_registry's `registry_credential` parameter.
    """
    client = boto3.client("ecr", region_name=region)
    response = client.get_authorization_token()
    auth_data = response["authorizationData"][0]
    decoded = base64.b64decode(auth_data["authorizationToken"]).decode("utf-8")
    username, secret = decoded.split(":", 1)
    logger.info("ecr_credential_generated", region=region)
    return {"username": username, "secret": secret}


def ensure_ecr_repository_exists(image_name: str, region: str) -> None:
    """
    Real gap found live (2026-09-16): unlike most registries, ECR does NOT
    auto-create a repository on first push — `docker push` to a repository
    name nobody ever ran `aws ecr create-repository` for just fails, and
    (worse, the failure mode actually hit) a project onboarded with a
    registry.internal-style placeholder never even reached ECR at all,
    since the image name wasn't real. A human onboarding a new AWS ECS
    project has no reason to know ECR needs this extra step — the build
    stage is the one place that already knows the real image name and is
    about to push to it, so it's the natural, safe place to make repo
    creation implicit rather than a manual prerequisite every project
    would otherwise need documented separately.

    Idempotent and safe to call on every build (create_repository's own
    "already exists" error is caught, not treated as a failure) — matches
    this codebase's established pattern for create-or-patch AWS/Kubernetes
    calls (e.g. deploy_task.py's `ensure_service`).
    """
    repo_name = image_name.split("/", 1)[1] if "/" in image_name else image_name
    client = boto3.client("ecr", region_name=region)
    try:
        client.create_repository(repositoryName=repo_name)
        logger.info("ecr_repository_created", repository=repo_name, region=region)
    except client.exceptions.RepositoryAlreadyExistsException:
        pass


def delete_ecr_repository(image_name: str, region: str) -> dict:
    """
    Real gap found live: a deleted project's build image (pushed here by `ensure_ecr_repository_exists` +
    the build stage) was left in ECR forever, still billing storage. `force=True` removes every image
    tag/layer in the repository too, not just the (now-orphaned) repository record — mirrors
    `deprovision_ecs_service`'s `force=True` on `delete_service` for the same reason: this repository
    existed for exactly this one project and nothing else references it. Idempotent: a non-ECR image name
    (a registry never created through this platform) or an already-deleted repository is reported as a
    no-op, never an error.
    """
    if not is_ecr_image(image_name):
        return {"status": "not_an_ecr_image"}
    repo_name = image_name.split("/", 1)[1] if "/" in image_name else image_name
    repo_name = repo_name.split(":", 1)[0]  # strip a tag, if the caller passed one
    client = boto3.client("ecr", region_name=region)
    try:
        client.delete_repository(repositoryName=repo_name, force=True)
        logger.info("ecr_repository_deleted", repository=repo_name, region=region)
        return {"status": "deleted", "repository": repo_name}
    except client.exceptions.RepositoryNotFoundException:
        return {"status": "already_deleted", "repository": repo_name}
