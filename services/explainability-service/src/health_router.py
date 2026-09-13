"""
services/explainability-service/src/health_router.py

/healthz + /readyz — spec §13.3. Readiness checks: Redis + a soft,
non-blocking check that GROQ_API_KEY is configured (a missing key never
fails readiness — report_generator.py always has a deterministic fallback).
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import structlog

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/healthz")
async def healthz():
    return {"status": "healthy", "service": "explainability-service"}


@router.get("/readyz")
async def readyz(request: Request):
    checks: dict[str, str] = {}

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Soft check only — never blocks readiness, since the RCA path has a fallback.
    checks["groq_api_key_configured"] = "ok" if os.environ.get("GROQ_API_KEY") else "not_configured"

    all_ok = checks["redis"] == "ok"
    return JSONResponse(status_code=200 if all_ok else 503, content={"ready": all_ok, "checks": checks})
