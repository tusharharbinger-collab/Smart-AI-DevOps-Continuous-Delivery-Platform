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

Real bug found live: `_set_status` used to (1) only write Redis's fast-path
`state:{run_id}` key, never the durable `execution_state` table Postgres
this platform treats as the source of truth everywhere else, and (2) never
checked the run's actual current status before flipping it — so clicking
"Resume" on a run that had already reached a genuinely terminal state
(FAILED, COMPLETED, ROLLED_BACK) silently faked the display back to
"RUNNING" with zero real work behind it: no worker thread is listening for
`pipeline:resume` once a run's execution has actually exited, so the UI
showed an indefinite, convincing "running" state that would never resolve.
Now reads the real status from Postgres first, rejects the transition with a
clear 409 if the run isn't actually resumable/pausable, and writes both
stores together so they can't drift apart again.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.auth.rbac import require_role
from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)


async def _set_status(
    request: Request, db: AsyncSession, pipeline_run_id: str, new_status: str, required_current_status: str
) -> dict:
    row = await db.execute(
        text("SELECT status FROM execution_state WHERE pipeline_run_id = :run_id"),
        {"run_id": pipeline_run_id},
    )
    current = row.mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="Pipeline run has no active execution state")
    if current["status"] != required_current_status:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot transition to {new_status}: run is {current['status']}, "
                f"not {required_current_status}. A run that has already reached a terminal "
                f"state (FAILED/COMPLETED/ROLLED_BACK) cannot be paused or resumed — trigger a new rollout instead."
            ),
        )

    now = datetime.now(timezone.utc)
    await db.execute(
        text("UPDATE execution_state SET status = :status, last_updated = :now WHERE pipeline_run_id = :run_id"),
        {"status": new_status, "now": now, "run_id": pipeline_run_id},
    )
    await db.commit()

    redis_client = request.app.state.redis
    raw = await redis_client.get(f"state:{pipeline_run_id}")
    state = json.loads(raw) if raw else {"pipeline_run_id": pipeline_run_id}
    state["status"] = new_status
    state["last_updated"] = now.isoformat()
    payload = json.dumps(state)
    await redis_client.set(f"state:{pipeline_run_id}", payload, ex=86400)
    # The WebSocket (src/websocket/event_stream.py) only sends the `state:`
    # key once, at connection time — a Pause/Resume click must also publish
    # to `events:{run_id}` or an already-open dashboard never sees the change.
    await redis_client.publish(f"events:{pipeline_run_id}", payload)
    return state


@router.post("/{pipeline_run_id}/pause", dependencies=[Depends(require_role("lead-sre"))])
async def pause_pipeline(pipeline_run_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Halts a rollout mid-shift (assignment §2b). No traffic weight changes
    occur while paused — the canary stays exactly at its last committed weight.
    Only valid from RUNNING — pausing a run that isn't actively running
    (already FAILED, already PAUSED, etc.) doesn't mean anything real.
    """
    state = await _set_status(request, db, pipeline_run_id, "PAUSED", required_current_status="RUNNING")
    logger.info("pipeline_paused", pipeline_run_id=pipeline_run_id)
    return {"status": "PAUSED", "pipeline_run_id": pipeline_run_id, "state": state}


@router.post("/{pipeline_run_id}/resume", dependencies=[Depends(require_role("lead-sre"))])
async def resume_pipeline(pipeline_run_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """Only valid from PAUSED — see module docstring for the real bug this guards against."""
    state = await _set_status(request, db, pipeline_run_id, "RUNNING", required_current_status="PAUSED")
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
