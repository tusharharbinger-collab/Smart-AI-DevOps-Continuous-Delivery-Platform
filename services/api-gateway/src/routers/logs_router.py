"""
services/api-gateway/src/routers/logs_router.py

SSE endpoint for execution logs — spec §11.1.

Polls the durable `logs:{pipeline_run_id}` Redis LIST (pipeline-worker's
`_log()`) rather than subscribing to a pub/sub channel. Real bug found
live: the original pub/sub design lost every line for any run a viewer
wasn't ALREADY subscribed to before it started — this platform's demo
pipeline completes in ~100ms, measurably faster than a client can open an
SSE connection, so pub/sub's fire-and-forget delivery meant the "Live
execution log" panel showed nothing for essentially every real click of
"Trigger New Rollout." A list is replayable: connecting after the run
already finished still shows its full log, and a still-running pipeline's
new lines keep streaming in as they're appended.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
import structlog

from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

POLL_INTERVAL_SECONDS = 0.5


@router.get("/{pipeline_run_id}/logs/stream")
async def stream_logs(pipeline_run_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    # Real bug found live (adversarial cross-tenant test): this endpoint used
    # to stream `logs:{run_id}` straight from Redis with no tenant check at
    # all — nothing stopped tenant B from watching tenant A's live execution
    # log just by knowing/guessing its run_id. `db` is the RLS-scoped
    # session, so a zero-row result means "not mine or doesn't exist."
    owns_run = await db.execute(
        text("SELECT 1 FROM pipeline_executions WHERE pipeline_run_id = :run_id"),
        {"run_id": pipeline_run_id},
    )
    if owns_run.first() is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    redis_client = request.app.state.redis
    key = f"logs:{pipeline_run_id}"

    async def event_generator():
        next_index = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                lines = await redis_client.lrange(key, next_index, -1)
            except Exception as e:
                logger.error("log_poll_failed", pipeline_run_id=pipeline_run_id, error=str(e))
                lines = []
            for line in lines:
                yield {"event": "log", "data": line}
            next_index += len(lines)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return EventSourceResponse(event_generator())
