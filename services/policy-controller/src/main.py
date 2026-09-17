"""
services/policy-controller/src/main.py

FastAPI entrypoint for the Policy Controller. Exposes /healthz + /readyz
(§13.3) and, on startup, launches controller.py's
`run_policy_controller_loop()` as a background asyncio task — this is the
process that actually subscribes to `verdicts:*` on Redis, verifies each
verdict's HMAC signature (§8.3), evaluates it against OPA (§8.4), and
actuates traffic-weight changes or rollbacks (§5.3) when authorized.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator

from shared.logging_config import configure_logging

configure_logging("policy-controller")

from shared import redis_streams as streams
from src.controller import (
    run_policy_controller_loop,
    handle_approval,
    _get_actuation_target,
    STREAM_VERDICTS,
    GROUP_POLICY_CONTROLLERS,
)
from src.actuation_executor import emergency_rollback
from src.aws_actuation_executor import emergency_rollback_ecs
from src.alert_dispatcher import alert_rollback
from src.db import PolicyControllerDB
from src.health_router import router as health_router
from src.platform_health_monitor import platform_health_monitor_loop

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

METRICS_UPDATE_INTERVAL_SECONDS = 10
# Phase 6 (§06-observability-platform-ops.md, 6.2): same "how deep is the
# queue" question as pipeline-worker's, for the verdict-processing side.
QUEUE_DEPTH_GAUGE = Gauge("verdict_queue_depth", "Total entries on the verdicts stream")
QUEUE_PENDING_GAUGE = Gauge("verdict_queue_pending", "Entries delivered to a consumer but not yet acked")


async def _metrics_update_loop(app: FastAPI):
    while True:
        try:
            QUEUE_DEPTH_GAUGE.set(await streams.stream_length(app.state.redis, STREAM_VERDICTS))
            QUEUE_PENDING_GAUGE.set(
                await streams.pending_count(app.state.redis, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("metrics_update_loop_failed", error=str(e))
        await asyncio.sleep(METRICS_UPDATE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    app.state.db = PolicyControllerDB()
    await app.state.db.connect()

    tasks = [
        asyncio.create_task(run_policy_controller_loop(db=app.state.db)),
        asyncio.create_task(_metrics_update_loop(app)),
        asyncio.create_task(platform_health_monitor_loop()),
    ]
    logger.info("policy_controller_startup")
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await app.state.db.close()
    await app.state.redis.aclose()
    logger.info("policy_controller_shutdown")


app = FastAPI(title="Policy Controller", version="1.0.0", lifespan=lifespan)
app.include_router(health_router, tags=["health"])
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.post("/internal/approvals/{run_id}")
async def approve_pending_promotion(run_id: str, body: dict):
    """
    Called by api-gateway's `POST /api/v1/projects/{id}/runs/{run_id}/approve`
    (itself RBAC-gated to lead-sre/platform-admin) once a human clicks
    "Approve" on a promotion paused at a `requiresManualApproval` step.
    Unauthenticated at this layer like every other internal service-to-
    service call in this platform (see that endpoint's docstring) — the
    real authorization check already happened in api-gateway before this
    was ever called.
    """
    result = await handle_approval(
        pipeline_run_id=run_id,
        approver_role=body.get("approver_role", "lead-sre"),
        approver_user=body.get("approver_user"),
        redis_client=app.state.redis,
        db=app.state.db,
    )
    return result


@app.post("/internal/manual-rollback/{run_id}")
async def manual_rollback(run_id: str, body: dict):
    """
    Real gap found live: projects_router.py's "Emergency Rollback" button
    used to only publish to a `pipeline:manual_rollback` Redis channel that
    NOTHING in this codebase ever subscribed to — clicking it updated a UI
    status label and never touched Kubernetes at all. This calls the exact
    same `emergency_rollback` an autonomous FAILED-verdict rollback calls,
    so a manual rollback is genuinely real: canary traffic to 0%, canary
    Deployment scaled down, a real signed audit_ledger row.
    """
    tenant_id = body.get("tenant_id")
    requested_by = body.get("requested_by", "operator")
    target = await _get_actuation_target(app.state.redis, run_id)

    if target.get("deployment_target", "kubernetes") == "aws_ecs":
        await emergency_rollback_ecs(
            run_id,
            authorized_by=f"MANUAL:{requested_by}",
            service_name=target["canary_deployment_name"].removesuffix("-canary"),
            path_prefix=target["path_prefix"],
            region=target.get("aws_region", "us-east-1"),
            tenant_id=tenant_id or target.get("tenant_id"),
            db=app.state.db,
        )
    else:
        await emergency_rollback(
            run_id,
            authorized_by=f"MANUAL:{requested_by}",
            route_name=target["route_name"],
            namespace=target["namespace"],
            canary_deployment_name=target["canary_deployment_name"],
            tenant_id=tenant_id or target.get("tenant_id"),
            db=app.state.db,
        )
    await alert_rollback(run_id, reason=f"Manual rollback requested by {requested_by}")
    return {"status": "ROLLED_BACK", "pipeline_run_id": run_id}
