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
from src.controller import run_policy_controller_loop, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS
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
