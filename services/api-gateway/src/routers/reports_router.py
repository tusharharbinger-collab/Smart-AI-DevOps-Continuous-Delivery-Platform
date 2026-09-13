"""
services/api-gateway/src/routers/reports_router.py

Report 1 (per-deployment verification report, §12.1) and Report 3
(periodic delivery-health digest, §12.3). Report 2 (Action Decision Report)
and the Groq RCA summary are produced by explainability-service;
this router assembles the raw evidence Report 1 needs and proxies to
explainability-service for the digest.
"""
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

EXPLAINABILITY_SERVICE_URL = os.environ.get(
    "EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004"
)


@router.get("/deployment/{run_id}")
async def get_deployment_report(run_id: str, db: AsyncSession = Depends(get_request_db)):
    """
    Report 1: what the telemetry showed. Every entry in `metric_evidence`
    already carries its exact-citation string (built by
    explainability-service's citation_builder at verdict-evidence-assembly time).
    """
    result = await db.execute(
        text(
            """
            SELECT verdict_id, status, composite_score, confidence, evidence,
                   tier1_breaches, rca_summary, timestamp_utc
            FROM verification_records
            WHERE pipeline_run_id = :run_id
            ORDER BY timestamp_utc DESC
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    )
    verdict = result.mappings().first()
    if verdict is None:
        raise HTTPException(status_code=404, detail="No verification record for this run")

    cost_result = await db.execute(
        text(
            "SELECT baseline_cost, canary_cost, delta_percent, rightsizing_rec "
            "FROM cost_analysis WHERE pipeline_run_id = :run_id "
            "ORDER BY computed_at DESC LIMIT 1"
        ),
        {"run_id": run_id},
    )
    cost = cost_result.mappings().first()

    return {
        "report_id": str(verdict["verdict_id"]),
        "pipeline_run_id": run_id,
        "final_verdict": verdict["status"],
        "confidence": float(verdict["confidence"]),
        "composite_score": float(verdict["composite_score"]),
        "metric_evidence": verdict["evidence"],
        "tier1_breaches": verdict["tier1_breaches"],
        "rca_summary": verdict["rca_summary"],
        "cost_analysis": dict(cost) if cost else None,
        "timestamp_utc": verdict["timestamp_utc"].isoformat(),
    }


@router.get("/digest/{tenant_id}")
async def get_delivery_health_digest(tenant_id: str, request: Request, days: int = 7):
    """
    Report 3: proxies to explainability-service's digest_generator.

    Real bug found live (adversarial cross-tenant test): `tenant_id` came
    straight from the URL with no check against the caller's own JWT
    tenant_id — a plain IDOR letting any authenticated tenant read any other
    tenant's full digest (rollback rate, cost trends, MTTV) just by knowing
    a UUID, no run_id guessing required. explainability-service has no JWT
    context of its own to check this, so the gate has to be here.
    """
    caller_tenant_id = getattr(request.state, "tenant_id", None)
    if caller_tenant_id is None or str(caller_tenant_id) != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access another tenant's digest")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{EXPLAINABILITY_SERVICE_URL}/digest/{tenant_id}", params={"days": days}
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        logger.error("digest_proxy_failed", error=str(e), tenant_id=tenant_id)
        raise HTTPException(status_code=502, detail=f"Digest generation unavailable: {e}")
