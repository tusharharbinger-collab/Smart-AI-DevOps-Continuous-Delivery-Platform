"""
services/api-gateway/src/routers/actuation_router.py

Pause / resume / manual-rollback endpoints (spec §4.3, §5.3). These endpoints
never touch Kubernetes directly — the api-gateway only ever writes execution
state; the actual HTTPRoute weight patch is performed exclusively by
policy-controller's actuation_executor.py after an OPA `allow_action=true`.

Pause/resume works by flipping `execution_state.status`: every Celery tick in
pipeline-worker's progressive_verify loop checks status == PAUSED before
advancing and simply no-ops while paused (see pipeline-worker/src/pipeline
/execution_state.py + reconciler.py).
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
import structlog

from src.auth.rbac import require_role

router = APIRouter()
logger = structlog.get_logger(__name__)


async def _set_status(request: Request, pipeline_run_id: str, status: str) -> dict:
    redis_client = request.app.state.redis
    raw = await redis_client.get(f"state:{pipeline_run_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Pipeline run has no active execution state")

    state = json.loads(raw)
    state["status"] = status
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(state)
    await redis_client.set(f"state:{pipeline_run_id}", payload, ex=86400)
    # The WebSocket (src/websocket/event_stream.py) only sends the `state:`
    # key once, at connection time — a Pause/Resume click must also publish
    # to `events:{run_id}` or an already-open dashboard never sees the change.
    await redis_client.publish(f"events:{pipeline_run_id}", payload)
    return state


@router.post("/{pipeline_run_id}/pause", dependencies=[Depends(require_role("lead-sre"))])
async def pause_pipeline(pipeline_run_id: str, request: Request):
    """
    Halts a rollout mid-shift (assignment §2b). No traffic weight changes
    occur while paused — the canary stays exactly at its last committed weight.
    """
    state = await _set_status(request, pipeline_run_id, "PAUSED")
    logger.info("pipeline_paused", pipeline_run_id=pipeline_run_id)
    return {"status": "PAUSED", "pipeline_run_id": pipeline_run_id, "state": state}


@router.post("/{pipeline_run_id}/resume", dependencies=[Depends(require_role("lead-sre"))])
async def resume_pipeline(pipeline_run_id: str, request: Request):
    state = await _set_status(request, pipeline_run_id, "RUNNING")
    redis_client = request.app.state.redis
    await redis_client.publish("pipeline:resume", pipeline_run_id)
    logger.info("pipeline_resumed", pipeline_run_id=pipeline_run_id)
    return {"status": "RUNNING", "pipeline_run_id": pipeline_run_id, "state": state}


@router.post("/{pipeline_run_id}/rollback", dependencies=[Depends(require_role("lead-sre"))])
async def request_emergency_rollback(pipeline_run_id: str, request: Request):
    """
    Manual "Emergency Rollback" button (Screen 1). This does NOT call
    Kubernetes directly — it publishes a synthetic FAILED-status control
    message that policy-controller treats identically to an autonomous
    verification-triggered rollback: it still goes through HMAC verification
    and OPA (Rule 1, autoRollbackOnVerdict) before anything is actuated.
    """
    redis_client = request.app.state.redis
    await redis_client.publish(
        "pipeline:manual_rollback",
        json.dumps(
            {
                "pipeline_run_id": pipeline_run_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "requested_by": "manual_operator",
            }
        ),
    )
    logger.warning("manual_rollback_requested", pipeline_run_id=pipeline_run_id)
    return {"status": "ROLLBACK_REQUESTED", "pipeline_run_id": pipeline_run_id}
