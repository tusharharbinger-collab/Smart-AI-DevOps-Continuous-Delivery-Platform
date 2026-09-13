"""
services/api-gateway/src/routers/verification_router.py

Serves verification verdicts + metric evidence for Screen 2
(Verification Detail View, §11.2). Verdicts are produced by the
verification-engine, HMAC-signed, and published on the Redis channel
`verdicts:{pipeline_run_id}`; the gateway also mirrors the most recent
verdict per run into `verdict:{pipeline_run_id}` (a plain key, not pub/sub)
so a REST GET can serve it without needing to have been subscribed live.
"""
import json

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
import structlog

from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)


async def _assert_run_belongs_to_caller_tenant(pipeline_run_id: str, db: AsyncSession) -> None:
    """
    Real bug found live (adversarial cross-tenant test): both endpoints below
    used to read straight from Redis (`verdict:{run_id}`) with no tenant
    check at all — the signed verdict payload itself carries no tenant_id
    (verification-engine is structurally barred from tenant/k8s concerns,
    see invariant #3), so nothing stopped tenant B from reading tenant A's
    verdict just by knowing/guessing its run_id. `db` here is the RLS-scoped
    session (`get_request_db`), so this SELECT physically cannot see a row
    belonging to another tenant — zero rows means "not mine or doesn't
    exist," which is exactly the 404 we want to return either way.
    """
    result = await db.execute(
        text("SELECT 1 FROM pipeline_executions WHERE pipeline_run_id = :run_id"),
        {"run_id": pipeline_run_id},
    )
    if result.first() is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")


@router.get("/{pipeline_run_id}")
async def get_verification_result(
    pipeline_run_id: str, request: Request, db: AsyncSession = Depends(get_request_db)
):
    """
    Returns the latest verdict for a pipeline run: status, confidence,
    composite score, and the per-metric evidence dict (which includes the
    exact-citation strings built by explainability-service's citation_builder).
    """
    await _assert_run_belongs_to_caller_tenant(pipeline_run_id, db)

    redis_client = request.app.state.redis
    cached = await redis_client.get(f"verdict:{pipeline_run_id}")
    if not cached:
        raise HTTPException(status_code=404, detail="No verdict published yet for this run")

    payload = json.loads(cached)
    verdict = json.loads(payload["verdict"]) if isinstance(payload.get("verdict"), str) else payload
    return verdict


@router.get("/{pipeline_run_id}/history")
async def get_verification_history(
    pipeline_run_id: str, db: AsyncSession = Depends(get_request_db)
):
    """Durable history of every verdict recorded for this run (Postgres)."""
    await _assert_run_belongs_to_caller_tenant(pipeline_run_id, db)

    result = await db.execute(
        text(
            """
            SELECT verdict_id, status, composite_score, confidence, evidence,
                   tier1_breaches, rca_summary, timestamp_utc
            FROM verification_records
            WHERE pipeline_run_id = :run_id
            ORDER BY timestamp_utc DESC
            """
        ),
        {"run_id": pipeline_run_id},
    )
    return {"records": [dict(r) for r in result.mappings().all()]}
