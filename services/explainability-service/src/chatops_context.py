"""
services/explainability-service/src/chatops_context.py

Assembles the ONLY data `chatops_answerer.py` is ever allowed to see —
capped to a project's last N real runs, tenant/project-scoped by the same
RLS the rest of this service already relies on (`SET LOCAL
app.active_tenant_id`, set by the caller before this runs, same
transaction — see main.py's existing `/digest/{tenant_id}` endpoint for
the identical pattern this mirrors).

Reuses `projects_router.py::list_project_runs`'s exact join shape (real
status via COALESCE(execution_state.status, pipeline_executions.status) —
`pipeline_executions.status` alone is written once as 'PENDING' and never
updated again, a real trap this codebase has already hit once) plus
`audit_ledger`, which is the only table that says whether a rollback
actually FIRED and what authorized it — a verdict alone doesn't say that.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_RUNS = 5
MAX_AUDIT_ROWS_PER_RUN = 5


async def assemble_chatops_context(db: AsyncSession, tenant_id: str, project_id: str) -> dict:
    runs_result = await db.execute(
        text(
            """
            SELECT e.pipeline_run_id, e.commit_sha, e.commit_message, e.trigger_type,
                   COALESCE(es.status, e.status) AS status,
                   COALESCE(es.current_stage, e.current_stage) AS current_stage,
                   e.started_at, e.completed_at,
                   v.status AS verdict, v.confidence, v.rca_summary
            FROM pipeline_executions e
            LEFT JOIN execution_state es ON es.pipeline_run_id = e.pipeline_run_id
            LEFT JOIN LATERAL (
                SELECT status, confidence, rca_summary
                FROM verification_records
                WHERE pipeline_run_id = e.pipeline_run_id
                ORDER BY timestamp_utc DESC
                LIMIT 1
            ) v ON TRUE
            WHERE e.project_id = :project_id AND e.tenant_id = :tenant_id
            ORDER BY e.started_at DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id, "limit": MAX_RUNS},
    )
    runs = [dict(r) for r in runs_result.mappings().all()]
    run_ids = [str(r["pipeline_run_id"]) for r in runs]
    if not run_ids:
        return {"project_id": project_id, "runs": []}

    audit_result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, action, verdict, confidence, authorized_by, "timestamp"
            FROM audit_ledger
            WHERE pipeline_run_id = ANY(:run_ids) AND tenant_id = :tenant_id
            ORDER BY pipeline_run_id, "timestamp"
            """
        ),
        {"run_ids": run_ids, "tenant_id": tenant_id},
    )
    audit_by_run: dict[str, list[dict]] = {}
    for row in audit_result.mappings().all():
        key = str(row["pipeline_run_id"])
        audit_by_run.setdefault(key, [])
        if len(audit_by_run[key]) < MAX_AUDIT_ROWS_PER_RUN:
            audit_by_run[key].append(
                {
                    "action": row["action"],
                    "verdict": row["verdict"],
                    "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                    "authorized_by": row["authorized_by"],
                    "timestamp": row["timestamp"],
                }
            )

    cost_result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, baseline_cost, canary_cost, delta_percent
            FROM cost_analysis
            WHERE pipeline_run_id = ANY(:run_ids) AND tenant_id = :tenant_id
            """
        ),
        {"run_ids": run_ids, "tenant_id": tenant_id},
    )
    cost_by_run = {
        str(row["pipeline_run_id"]): {
            "baseline_cost": float(row["baseline_cost"]),
            "canary_cost": float(row["canary_cost"]),
            "delta_percent": float(row["delta_percent"]),
        }
        for row in cost_result.mappings().all()
    }

    for run in runs:
        run_id = str(run["pipeline_run_id"])
        run["audit_actions"] = audit_by_run.get(run_id, [])
        run["cost"] = cost_by_run.get(run_id)

    return {"project_id": project_id, "runs": runs}
