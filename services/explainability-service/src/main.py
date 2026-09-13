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

from src.citation_builder import build_citations_from_engine_evidence
from src.db import get_db
from src.decision_report import build_decision_report
from src.digest_generator import generate_delivery_health_digest
from src.health_router import router as health_router
from src.report_generator import generate_rca

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


@app.get("/digest/{tenant_id}")
async def get_digest(tenant_id: str, days: int = 7, db: AsyncSession = Depends(get_db)):
    # Must run in the same transaction as the queries digest_generator.py
    # issues below — set_config(..., true) ("SET LOCAL" semantics) only
    # lasts for the current transaction, so this can't be wrapped in its own
    # `async with db.begin():` block (that would commit and close the
    # transaction before the real queries ever ran).
    from sqlalchemy import text

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    return await generate_delivery_health_digest(db, tenant_id, days)
