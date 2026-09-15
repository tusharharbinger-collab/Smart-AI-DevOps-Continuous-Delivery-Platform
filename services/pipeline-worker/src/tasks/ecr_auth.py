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
