"""
services/policy-controller/src/health_router.py

/healthz + /readyz — spec §13.3. Readiness checks Redis + OPA (Kubernetes
API reachability is checked best-effort and never fails readiness outright,
since a transient kubeconfig/API-server hiccup shouldn't take the whole
controller out of rotation for handling non-actuating verdicts).
"""
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import structlog

router = APIRouter()
logger = structlog.get_logger(__name__)

OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")


@router.get("/healthz")
async def healthz():
    return {"status": "healthy", "service": "policy-controller"}


@router.get("/readyz")
async def readyz(request: Request):
    checks: dict[str, str] = {}

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{OPA_URL}/health")
            checks["opa"] = "ok" if resp.status_code == 200 else f"error: status {resp.status_code}"
    except Exception as e:
        checks["opa"] = f"error: {e}"

    all_ok = checks.get("redis") == "ok" and checks.get("opa") == "ok"
    return JSONResponse(status_code=200 if all_ok else 503, content={"ready": all_ok, "checks": checks})
