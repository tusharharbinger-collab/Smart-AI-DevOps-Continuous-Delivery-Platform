"""
services/explainability-service/src/digest_generator.py

Report 3: Periodic Delivery-Health Digest — spec §12.3. Summarizes rollback
rate, mean-time-to-verify, and average cost delta for a tenant over a
trailing window. Read-only, RLS-scoped by tenant_id in every query.
"""
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def generate_delivery_health_digest(
    db: AsyncSession, tenant_id: str, days: int = 7
) -> dict:
    result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, status, started_at, completed_at
            FROM pipeline_executions
            WHERE tenant_id = :tenant_id
              AND started_at >= now() - (:days * interval '1 day')
            """
        ),
        {"tenant_id": tenant_id, "days": days},
    )
    runs = result.mappings().all()

    rollbacks = [r for r in runs if r["status"] == "ROLLED_BACK"]

    first_verdict_result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, MIN(timestamp_utc) AS first_verdict_at
            FROM verification_records
            WHERE tenant_id = :tenant_id
              AND timestamp_utc >= now() - (:days * interval '1 day')
            GROUP BY pipeline_run_id
            """
        ),
        {"tenant_id": tenant_id, "days": days},
    )
    first_verdict_by_run = {
        str(r["pipeline_run_id"]): r["first_verdict_at"] for r in first_verdict_result.mappings().all()
    }

    mttv_values: list[float] = []
    for run in runs:
        first_verdict_at = first_verdict_by_run.get(str(run["pipeline_run_id"]))
        if first_verdict_at and run["started_at"]:
            mttv_values.append((first_verdict_at - run["started_at"]).total_seconds())

    cost_result = await db.execute(
        text(
            """
            SELECT delta_percent
            FROM cost_analysis
            WHERE tenant_id = :tenant_id
              AND computed_at >= now() - (:days * interval '1 day')
            """
        ),
        {"tenant_id": tenant_id, "days": days},
    )
    cost_deltas = [float(r["delta_percent"]) for r in cost_result.mappings().all()]

    total = len(runs)
    return {
        "tenant_id": tenant_id,
        "period_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_deployments": total,
        "rollback_count": len(rollbacks),
        "rollback_rate_percent": round(len(rollbacks) / total * 100, 2) if total else 0,
        "mean_time_to_verify_seconds": (
            round(sum(mttv_values) / len(mttv_values), 1) if mttv_values else None
        ),
        "pipeline_success_rate_percent": (
            round((total - len(rollbacks)) / total * 100, 2) if total else 0
        ),
        "avg_cost_delta_percent": (
            round(sum(cost_deltas) / len(cost_deltas), 2) if cost_deltas else 0
        ),
    }
