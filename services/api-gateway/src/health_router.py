"""
services/api-gateway/src/health_router.py

/healthz + /readyz — spec §13.3. This exact shape is replicated across all
5 services, with only the readiness dependency checks varying per service.
api-gateway checks: Postgres + Redis + OPA.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
import httpx
import structlog

from src.config import settings

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/healthz")
async def healthz():
    """Liveness — always 200 if the process is up and can handle requests."""
    return {"status": "healthy", "service": settings.SERVICE_NAME}


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness — Postgres + Redis + OPA must all be reachable."""
    checks: dict[str, str] = {}

    try:
        engine = request.app.state.db_engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.OPA_URL}/health")
            checks["opa"] = "ok" if resp.status_code == 200 else f"error: status {resp.status_code}"
    except Exception as e:
        checks["opa"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    body = {"ready": all_ok, "checks": checks}
    return JSONResponse(status_code=200 if all_ok else 503, content=body)
