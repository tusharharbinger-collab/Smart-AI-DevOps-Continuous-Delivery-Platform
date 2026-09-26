"""
services/policy-controller/src/rca_trigger.py

Fires RCA (root-cause-analysis) generation via explainability-service AFTER
policy-controller has already finished acting on a verdict. Found live: a
user asked how they'd know what happened to their code, and tracing that
question all the way through found explainability-service's `/rca`
endpoint had NO caller anywhere in the real pipeline flow — it existed,
worked, but nothing ever invoked it, so no run has ever gotten a plain-
language explanation.

Deliberately fire-and-forget (`asyncio.create_task`, never awaited inline):
explainability-service's own docstring is explicit that "the rollback/
promotion decision has ALREADY been executed... a Groq outage must never
block or delay the actual safety action" — generating the explanation is
never allowed to slow down or risk the actuation path it explains. If it
fails (Groq down, timeout, whatever), the run still has its verdict/audit
trail; it just won't have a human-readable summary attached.
"""
import asyncio
import os

import httpx
import structlog

import json

from src.rca_context_collector import gather_rca_context

logger = structlog.get_logger(__name__)

EXPLAINABILITY_SERVICE_URL = os.environ.get("EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004")


async def _generate_and_store_rca(db, tenant_id: str | None, verdict: dict, action: str) -> None:
    verdict_id = verdict.get("verdict_id")
    pipeline_run_id = verdict.get("pipeline_run_id")
    try:
        # Collect multimodal context (commit diff + container error logs) safely
        extra_ctx = {}
        if db and tenant_id and pipeline_run_id:
            try:
                extra_ctx = await gather_rca_context(db, tenant_id, pipeline_run_id)
            except Exception as e:
                logger.warning("rca_gather_context_failed_softly", error=str(e), pipeline_run_id=pipeline_run_id)

        analysis_data = {
            "verdict_id": verdict_id,
            "pipeline_run_id": pipeline_run_id,
            "service_name": extra_ctx.get("service_name"),
            "commit_sha": extra_ctx.get("commit_sha"),
            "commit_message": extra_ctx.get("commit_message"),
            "commit_diff": extra_ctx.get("commit_diff"),
            "error_logs": extra_ctx.get("error_logs", []),
            "final_verdict": verdict.get("status"),
            "composite_score": verdict.get("composite_score"),
            "confidence": verdict.get("confidence"),
            "metric_evidence": verdict.get("evidence", {}),
            "tier1_breaches": verdict.get("tier1_breaches", []),
            "action_taken": action,
        }
        endpoint = "/degradation-diagnosis" if verdict.get("status") == "DEGRADED" else "/rca"
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(f"{EXPLAINABILITY_SERVICE_URL}{endpoint}", json=analysis_data)
            resp.raise_for_status()
            rca = resp.json()

        # Check that we got a valid response with an executive summary
        summary = rca.get("executive_summary") if isinstance(rca, dict) else None
        if not summary:
            logger.warning("rca_generated_but_no_summary_field", pipeline_run_id=pipeline_run_id, raw=rca)
            return

        # Store complete structured RCA (as JSON) so the UI can render
        # the git diff, error log excerpt, and file/line diagnosis
        stored_payload = json.dumps(rca) if isinstance(rca, dict) else summary

        if db and tenant_id and verdict_id:
            await db.update_rca_summary(tenant_id=tenant_id, verdict_id=verdict_id, rca_summary=stored_payload)
            logger.info("rca_stored", pipeline_run_id=pipeline_run_id, verdict_id=verdict_id)
        else:
            logger.warning(
                "rca_generated_but_not_stored",
                pipeline_run_id=pipeline_run_id,
                reason="missing db/tenant_id/verdict_id",
            )
    except Exception as e:
        logger.error("rca_generation_failed", pipeline_run_id=pipeline_run_id, error=str(e))


async def trigger_rca_async(db, tenant_id: str | None, verdict: dict, action: str) -> None:
    """Schedules RCA generation and returns immediately — never delays the caller."""
    asyncio.create_task(_generate_and_store_rca(db, tenant_id, verdict, action))
