"""
services/api-gateway/src/main.py

FastAPI entrypoint. Wires together: structured logging, the tenant-context
RLS middleware, every REST router, the WebSocket event stream, and the
health/readiness endpoints. Spec §6 (Phase 6 / api-gateway).
"""
import asyncio
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from shared.logging_config import configure_logging

configure_logging("api-gateway")

import structlog

from src.auth.middleware import tenant_context_middleware
from src.config import settings
from src.db.session import engine
from src.health_router import router as health_router
from src.routers.actuation_router import router as actuation_router
from src.routers.auth_router import router as auth_router
from src.routers.audit_router import router as audit_router
from src.routers.github_router import router as github_router
from src.routers.logs_router import router as logs_router
from src.routers.pipeline_router import router as pipeline_router
from src.routers.policy_router import router as policy_router
from src.routers.projects_router import router as projects_router
from src.routers.registry_router import router as registry_router
from src.routers.reports_router import router as reports_router
from src.routers.services_router import router as services_router
from src.routers.verification_router import router as verification_router
from src.routers.webhooks_router import (
    router as webhooks_router,
    poll_for_missed_webhook_deliveries,
    WEBHOOK_POLL_INTERVAL_SECONDS,
)
from src.websocket.event_stream import router as event_stream_router

logger = structlog.get_logger(__name__)


async def _webhook_poll_loop(app: FastAPI):
    """
    Webhook polling fallback (P0, 2026-09-16) — periodic safety net for a
    delivery GitHub itself never retried, or this platform being
    unreachable when it tried. See webhooks_router.py's
    poll_for_missed_webhook_deliveries for the real comparison logic; this
    is just the loop shape (sleep, run, never let one bad cycle kill the
    task — the same "log and keep looping" pattern pipeline-worker's
    reconcile loop already uses).
    """
    while True:
        await asyncio.sleep(WEBHOOK_POLL_INTERVAL_SECONDS)
        try:
            await poll_for_missed_webhook_deliveries(app.state.redis)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("webhook_poll_loop_failed", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_engine = engine
    app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    logger.info("api_gateway_startup", redis_url=settings.REDIS_URL)
    poll_task = asyncio.create_task(_webhook_poll_loop(app))
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await app.state.redis.aclose()
    await engine.dispose()
    logger.info("api_gateway_shutdown")


app = FastAPI(
    title="Smart AI DevOps & Continuous Delivery Platform — API Gateway",
    version="1.0.0",
    lifespan=lifespan,
)

app.middleware("http")(tenant_context_middleware)

# CORSMiddleware must be added LAST: Starlette builds the middleware stack so
# that whichever middleware is added last ends up OUTERMOST (runs first on
# the way in). Adding it before tenant_context_middleware would let the
# tenant middleware intercept the browser's CORS preflight (OPTIONS, no
# Authorization header) first and reject it with 401 — before CORSMiddleware
# ever gets a chance to answer the preflight itself. That is exactly what a
# browser reports back to `fetch()` as an opaque "TypeError: Failed to fetch".
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health/readiness are unauthenticated (see auth/middleware.py's path allowlist).
app.include_router(health_router, tags=["health"])

app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(pipeline_router, prefix="/api/v1/pipelines", tags=["pipelines"])
app.include_router(verification_router, prefix="/api/v1/verification", tags=["verification"])
app.include_router(actuation_router, prefix="/api/v1/pipelines", tags=["actuation"])
app.include_router(policy_router, prefix="/api/v1/policies", tags=["policy"])
app.include_router(reports_router, prefix="/api/v1/reports", tags=["reports"])
app.include_router(audit_router, prefix="/api/v1/audit", tags=["audit"])
app.include_router(logs_router, prefix="/api/v1/pipelines", tags=["logs"])
app.include_router(services_router, prefix="/api/v1/services", tags=["services"])

# Phase 8 (§08-project-workspaces.md): Render-style project workspaces and
# the GitHub ingestion backing the project-creation wizard.
app.include_router(projects_router, prefix="/api/v1/projects", tags=["projects"])
app.include_router(github_router, prefix="/api/v1/integrations/github", tags=["github"])
app.include_router(registry_router, prefix="/api/v1/integrations/registry", tags=["registry"])

# Phase 9.6 (P0 #1, 2026-09-16) — the real "git push -> cloud" trigger.
# Deliberately unauthenticated (see auth/middleware.py's allowlist) — GitHub
# cannot send this platform's Authorization header; the HMAC signature is
# the real authentication here, verified inside the handler itself.
app.include_router(webhooks_router, prefix="/api/v1/webhooks", tags=["webhooks"])

# Phase 6 (§06-observability-platform-ops.md, deliverable 6.2): RED metrics
# (request rate/latency/error-rate per endpoint) at /metrics, scraped by the
# same Prometheus instance Phase 2 introduced (see monitoring/prometheus.yml).
Instrumentator().instrument(app).expose(app, include_in_schema=False)

app.include_router(event_stream_router, prefix="/ws/pipelines", tags=["websocket"])
