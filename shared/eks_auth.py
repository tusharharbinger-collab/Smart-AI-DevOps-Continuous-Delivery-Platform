"""
shared/eks_auth.py

Generates a Kubernetes client config for authenticating to a real AWS EKS
cluster via IAM, without needing the `aws` CLI binary inside any container
image — this reimplements the same token-exchange algorithm `aws eks
get-token` / aws-iam-authenticator use, using only `boto3`.

Used by every service's `_load_kube()` in place of a static kubeconfig file
(how the local Kind path works) — see actuation_executor.py, cost_tracker.py,
deploy_task.py, onboarding.py. Returns None when EKS_CLUSTER_NAME isn't set,
so every existing local/Kind code path is completely unaffected.

EKS bearer tokens expire in ~15 minutes, but every caller of this module
already re-runs `_load_kube()` from scratch on every single Kubernetes API
call (nothing caches a client across calls) — a token generated fresh each
time this is called is therefore always valid for the one short API call
that follows it, with no refresh logic needed.
"""
import base64
import os

import boto3
import structlog
from botocore.signers import RequestSigner

logger = structlog.get_logger(__name__)

STS_TOKEN_EXPIRES_IN_SECONDS = 60
EKS_TOKEN_PREFIX = "k8s-aws-v1."


def _generate_eks_token(cluster_name: str, region: str, session: "boto3.session.Session") -> str:
    sts_client = session.client("sts")
    service_id = sts_client.meta.service_model.service_id

    signer = RequestSigner(
        service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.events,
    )

    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }

    signed_url = signer.generate_presigned_url(
        params, region_name=region, expires_in=STS_TOKEN_EXPIRES_IN_SECONDS, operation_name=""
    )

    return EKS_TOKEN_PREFIX + base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8").rstrip("=")


def get_eks_kube_client_config() -> dict | None:
    """
    Returns a full kubeconfig dict (suitable for
    `kubernetes.config.load_kube_config_from_dict`) authenticated against a
    real EKS cluster via the caller's AWS IAM identity, or None if
    EKS_CLUSTER_NAME isn't configured — every caller falls back to its
    existing local/Kind behavior in that case.
    """
    cluster_name = os.environ.get("EKS_CLUSTER_NAME")
    if not cluster_name:
        return None

    region = os.environ.get("AWS_REGION", "us-east-1")
    session = boto3.session.Session(region_name=region)
    eks_client = session.client("eks")

    try:
        cluster = eks_client.describe_cluster(name=cluster_name)["cluster"]
    except Exception as e:
        logger.error("eks_describe_cluster_failed", cluster_name=cluster_name, error=str(e))
        raise

    endpoint = cluster["endpoint"]
    ca_data = cluster["certificateAuthority"]["data"]
    token = _generate_eks_token(cluster_name, region, session)

    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": cluster_name, "cluster": {"server": endpoint, "certificate-authority-data": ca_data}}],
        "contexts": [{"name": cluster_name, "context": {"cluster": cluster_name, "user": cluster_name}}],
        "current-context": cluster_name,
        "users": [{"name": cluster_name, "user": {"token": token}}],
    }
