"""
services/verification-engine/src/health_router.py

/healthz + /readyz — spec §13.3. Readiness checks Redis only — deliberately
NOT Kubernetes, since this service is structurally barred from any
Kubernetes access (see §8.1: no `kubernetes` dependency, no kubeconfig mount).
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import structlog

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/healthz")
async def healthz():
    return {"status": "healthy", "service": "verification-engine"}


@router.get("/readyz")
async def readyz(request: Request):
    checks: dict[str, str] = {}
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = checks.get("redis") == "ok"
    return JSONResponse(status_code=200 if all_ok else 503, content={"ready": all_ok, "checks": checks})
