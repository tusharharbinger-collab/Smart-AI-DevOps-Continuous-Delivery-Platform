"""
services/api-gateway/src/routers/audit_router.py

Screen 4 (Deployment History & Audit, §11.4) + Report 4 (Compliance/Audit
Export, §12.4). RLS scopes every query automatically via the tenant_id set
by auth/middleware.py — there is no explicit "only return my tenant's rows"
filter needed beyond passing tenant_id, since a cross-tenant row physically
cannot be returned by Postgres.
"""
import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

_FIELDNAMES = [
    "actuation_id", "timestamp", "action", "pipeline_run_id", "verdict",
    "confidence", "authorized_by", "policy_rule", "hmac_signature",
]


@router.get("")
async def list_audit_entries(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    db: AsyncSession = Depends(get_request_db),
):
    tenant_id = getattr(request.state, "tenant_id", None)
    query = """
        SELECT actuation_id, pipeline_run_id, action, verdict, confidence,
               authorized_by, policy_rule, hmac_signature, canary_weight,
               baseline_weight, timestamp
        FROM audit_ledger
        WHERE tenant_id = :tenant_id
    """
    params: dict = {"tenant_id": str(tenant_id)}
    if start:
        query += " AND timestamp >= :start"
        params["start"] = start
    if end:
        query += " AND timestamp <= :end"
        params["end"] = end
    query += " ORDER BY timestamp DESC"

    result = await db.execute(text(query), params)
    return {"entries": [dict(r) for r in result.mappings().all()]}


@router.get("/export/soc2")
async def export_soc2(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    db: AsyncSession = Depends(get_request_db),
):
    """Report 4: streams the tenant's audit ledger as a downloadable SOC 2 CSV."""
    tenant_id = getattr(request.state, "tenant_id", None)
    query = """
        SELECT actuation_id, timestamp, action, pipeline_run_id, verdict,
               confidence, authorized_by, policy_rule, hmac_signature
        FROM audit_ledger
        WHERE tenant_id = :tenant_id
    """
    params: dict = {"tenant_id": str(tenant_id)}
    if start:
        query += " AND timestamp >= :start"
        params["start"] = start
    if end:
        query += " AND timestamp <= :end"
        params["end"] = end
    query += " ORDER BY timestamp DESC"

    result = await db.execute(text(query), params)
    rows = result.mappings().all()

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_FIELDNAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) for k in _FIELDNAMES})
    buf.seek(0)

    logger.info("soc2_export_generated", tenant_id=tenant_id, row_count=len(rows))
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=soc2_export_{tenant_id}.csv"},
    )
