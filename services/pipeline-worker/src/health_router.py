"""
services/pipeline-worker/src/health_router.py

/healthz + /readyz — spec §13.3. Readiness checks Redis + the Kubernetes API
(pipeline-worker is one of the two services with kubeconfig access, the
other being policy-controller).
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import structlog

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/healthz")
async def healthz():
    return {"status": "healthy", "service": "pipeline-worker"}


@router.get("/readyz")
async def readyz(request: Request):
    checks: dict[str, str] = {}

    try:
        request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    try:
        from kubernetes import client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        client.VersionApi().get_code()
        checks["kubernetes"] = "ok"
    except Exception as e:
        checks["kubernetes"] = f"error: {e}"

    all_ok = checks.get("redis") == "ok"
    return JSONResponse(status_code=200 if all_ok else 503, content={"ready": all_ok, "checks": checks})
