"""
services/api-gateway/src/routers/services_router.py

Phase 3 (§03-multi-service-onboarding.md), deliverable 3.3 — a team
describes a new containerized HTTP service once; this endpoint generates
and applies its Kubernetes objects (via pipeline-worker, the only service
with a live kubeconfig for this purpose) and registers the resulting
Pipeline YAML exactly the way a hand-written one is registered today
(POST /api/v1/pipelines) — no separate onboarding-specific pipeline model.
"""
import os
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
import structlog

from src.auth.rbac import require_role
from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

PIPELINE_WORKER_URL = os.environ.get("PIPELINE_WORKER_URL", "http://pipeline-worker:8001")


class RegisterServiceRequest(BaseModel):
    service_name: str
    image: str
    baseline_tag: str
    canary_tag: str
    port: int = 8080
    health_check_path: str = "/healthz"
    path_prefix: str | None = None
    # Real gap found live (Phase 3 hardening): this defaulted to a single
    # hardcoded "production" for every tenant — every onboarded service
    # across every tenant landed in the exact same namespace, with nothing
    # enforcing isolation at the Kubernetes level (Postgres RLS already
    # isolates the *data*, but not the cluster resources). None (the
    # default) now means "derive one per tenant" — see register_service.
    # An explicit value here still overrides that, for anyone who genuinely
    # wants a shared namespace.
    namespace: str | None = None


def _get_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


@router.post("", dependencies=[Depends(require_role("lead-sre"))])
async def register_service(
    body: RegisterServiceRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Guided-onboarding backend: calls pipeline-worker to generate + apply the
    new service's Deployments/Services/HTTPRoute against the real cluster,
    then persists the generated pipeline the same way a hand-written
    POST /api/v1/pipelines call would — this endpoint is a convenience that
    produces exactly that, not a separate code path downstream.
    """
    tenant_id = _get_tenant_id(request)
    # Derive a per-tenant namespace when the caller didn't explicitly pick
    # one — the first UUID segment is always a valid Kubernetes namespace
    # name (lowercase hex + no separator issues) without an extra DB lookup
    # for the tenant's display name.
    resolved_namespace = body.namespace or f"tenant-{tenant_id.split('-')[0]}"

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        try:
            resp = await http_client.post(
                f"{PIPELINE_WORKER_URL}/services/onboard",
                json={**body.model_dump(), "tenant_id": tenant_id, "namespace": resolved_namespace},
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"pipeline-worker unreachable: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Onboarding failed: {resp.text}")

    onboarding_result = resp.json()
    policy_yaml = onboarding_result["pipeline_yaml"]

    pipeline_id = str(uuid.uuid4())
    try:
        await db.execute(
            text(
                """
                INSERT INTO pipelines (pipeline_id, tenant_id, name, policy_yaml)
                VALUES (:pipeline_id, :tenant_id, :name, :policy_yaml)
                """
            ),
            {
                "pipeline_id": pipeline_id,
                "tenant_id": tenant_id,
                "name": f"{body.service_name}-rollout",
                "policy_yaml": policy_yaml,
            },
        )
        await db.commit()
    except IntegrityError:
        # K8s manifests were already applied idempotently above (re-applying
        # them is safe — create-then-patch-on-409); only the pipeline
        # registration is genuinely a duplicate here.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A pipeline named '{body.service_name}-rollout' is already registered for this tenant.",
        )
    logger.info("service_onboarded_and_registered", pipeline_id=pipeline_id, service_name=body.service_name)

    return {
        "pipeline_id": pipeline_id,
        "service_name": body.service_name,
        "route_name": onboarding_result["route_name"],
        "namespace": onboarding_result["namespace"],
        "generated_pipeline_yaml": policy_yaml,
    }
