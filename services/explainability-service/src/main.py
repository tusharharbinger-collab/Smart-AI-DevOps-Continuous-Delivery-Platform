"""
services/explainability-service/src/main.py

FastAPI entrypoint for the Explainability Service: Groq-backed RCA (§9.2),
exact-citation evidence (§9.1), the Action Decision Report (§12.2), and the
Delivery-Health Digest (§12.3).
"""
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import AsyncSession

from shared.logging_config import configure_logging

configure_logging("explainability-service")

import os

import structlog

from src.chatops_answerer import answer_chatops_question
from src.chatops_context import assemble_chatops_context
from src.citation_builder import build_citations_from_engine_evidence
from src.db import get_db
from src.decision_report import build_decision_report
from src.digest_generator import generate_delivery_health_digest
from src.digest_summarizer import generate_digest_summary
from src.health_router import router as health_router
from src.pipeline_generator import PipelineGenerationError, generate_pipeline_yaml
from src.report_generator import generate_rca
from src.stage_failure_analyzer import generate_stage_failure_rca

logger = structlog.get_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("explainability_service_startup")
    yield
    await app.state.redis.aclose()
    logger.info("explainability_service_shutdown")


app = FastAPI(title="Explainability Service", version="1.0.0", lifespan=lifespan)
app.include_router(health_router, tags=["health"])
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.post("/rca")
async def post_generate_rca(analysis_data: dict):
    """Generates a grounded RCA via Groq (with deterministic fallback)."""
    return await generate_rca(analysis_data)


@app.post("/citations")
async def post_build_citations(evidence: dict):
    """Builds the exact-citation string set from a verification-engine evidence dict."""
    return {"citation": build_citations_from_engine_evidence(evidence)}


@app.post("/decision-report")
async def post_decision_report(body: dict):
    """
    body: {"pipeline_run_id": str, "verdict": dict, "opa_result": dict, "action_taken": str}
    """
    try:
        report = build_decision_report(
            pipeline_run_id=body["pipeline_run_id"],
            verdict=body["verdict"],
            opa_result=body["opa_result"],
            action_taken=body["action_taken"],
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")
    return report.to_dict()


@app.post("/generate-pipeline")
async def post_generate_pipeline(body: dict):
    """
    body: {"prompt": str, "current_yaml": str, "context": dict, "validation_error": str | None}
    Never auto-applies anything — the caller (api-gateway) is responsible for
    validating the result against pipeline-worker's real validator before it
    ever reaches a human's screen for review.
    """
    try:
        return await generate_pipeline_yaml(
            prompt=body["prompt"],
            current_yaml=body["current_yaml"],
            context=body.get("context", {}),
            validation_error=body.get("validation_error"),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")
    except PipelineGenerationError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/stage-failure-rca")
async def post_stage_failure_rca(body: dict):
    """body: {"run_id": str, "failed_stage": str, "error_message": str, "recent_logs": list[str]}"""
    try:
        return await generate_stage_failure_rca(
            run_id=body["run_id"],
            failed_stage=body["failed_stage"],
            error_message=body["error_message"],
            recent_logs=body.get("recent_logs", []),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")


@app.get("/digest/{tenant_id}")
async def get_digest(tenant_id: str, days: int = 7, db: AsyncSession = Depends(get_db)):
    # Must run in the same transaction as the queries digest_generator.py
    # issues below — set_config(..., true) ("SET LOCAL" semantics) only
    # lasts for the current transaction, so this can't be wrapped in its own
    # `async with db.begin():` block (that would commit and close the
    # transaction before the real queries ever ran).
    from sqlalchemy import text

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    digest = await generate_delivery_health_digest(db, tenant_id, days)
    # AI digest summary (P0, 2026-09-16) — grounded entirely in the numbers
    # digest_generator.py just computed above, never a second data source.
    # Wrapped so a Groq outage degrades the summary quality, never the
    # digest response itself (generate_digest_summary already guarantees
    # this internally via its own fallback, this is defense in depth).
    try:
        digest["ai_summary"] = await generate_digest_summary(digest)
    except Exception as e:
        logger.warning("digest_ai_summary_failed", tenant_id=tenant_id, error=str(e))
        digest["ai_summary"] = None
    return digest


@app.post("/chatops/ask")
async def post_chatops_ask(body: dict, db: AsyncSession = Depends(get_db)):
    """
    ChatOps query interface — the assignment's own named bonus item.
    body: {"tenant_id": str, "project_id": str, "question": str}. Same
    same-transaction SET LOCAL requirement as /digest/{tenant_id} above —
    assemble_chatops_context's queries must run under the same RLS scoping
    this sets.
    """
    from sqlalchemy import text

    tenant_id = body.get("tenant_id")
    project_id = body.get("project_id")
    question = body.get("question")
    if not tenant_id or not project_id or not question:
        raise HTTPException(status_code=422, detail="tenant_id, project_id, and question are required")

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    context = await assemble_chatops_context(db, tenant_id, project_id)
    context["project_id"] = project_id
    return await answer_chatops_question(context, question)
